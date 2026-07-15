"""
On-chip feature constants + the table-based reference implementation.

The CMSIS-DSP port never derives its constants on-chip: the Hann window, the
shared 40-band mel filterbank, the DCT matrix, and the chroma filterbank are
computed HERE -- by the same librosa/scipy that produced the golden vector --
and frozen into generated C headers (gen_tables.py). extract_features_ref()
then recomputes the whole pipeline from ONLY those tables plus primitive
loops; it is the algorithm blueprint the C code mirrors, and the tests prove
it reproduces extract.py's librosa output. A wrong decomposition fails here,
on the laptop, never on the chip.
"""

import numpy as np
import numpy.typing as npt
import librosa
import scipy.fftpack

from offdevice.features import extract, params

F32Arr = npt.NDArray[np.float32]

# STFT geometry implied by the frozen contract: librosa center=True pads
# n_fft//2 zeros on each side (pad_mode='constant'), so frame t covers
# signal[t*hop - PAD, t*hop - PAD + n_fft) with zeros outside the window.
PAD = params.N_FFT // 2
N_FRAMES = 1 + params.WINDOW_BYTES // params.HOP_LENGTH
N_SPEC_BINS = params.N_FFT // 2 + 1

# librosa internals the frozen calls imply, pinned so the C port and the
# reference share one source. power_to_db floors the mel power at AMIN before
# log10, then clamps dB at (max - TOP_DB) -- the max over the WHOLE 40x33
# matrix, not per frame.
AMIN = 1e-10
TOP_DB = 80.0
# util.normalize(norm=inf) guard: a chroma frame whose peak is below
# float32-tiny is left undivided (divisor 1), never divided by ~0.
NORM_TINY = float(np.finfo(np.float32).tiny)


def hann_window() -> F32Arr:
    """The periodic Hann window librosa.stft applies to every frame."""
    # librosa keeps the window float64 end-to-end; the chip multiplies in
    # float32, so the table is the float64 window rounded once.
    win = librosa.filters.get_window("hann", params.N_FFT, fftbins=True)
    return win.astype(np.float32)


def mel_filterbank() -> F32Arr:
    """The ONE 40-band mel filterbank both the MFCC and mel features consume."""
    # The single-bank design stands on params pinning MFCC's internal bank to
    # the standalone mel's; refuse to generate if that identity ever breaks.
    if (params.MFCC_INTERNAL_N_MELS, params.MFCC_FMAX) != (params.N_MELS, params.FMAX):
        raise ValueError("MFCC's internal mel bank no longer equals the standalone "
                         "mel's -- the shared on-chip bank is invalid")
    # Exactly the internal librosa.filters.mel call (defaults: fmin=0,
    # htk=False, norm='slaney') -- and float32 natively, so this table is
    # bit-identical to the bank librosa itself used for the golden.
    return librosa.filters.mel(sr=params.SR, n_fft=params.N_FFT,
                               n_mels=params.N_MELS, fmax=params.FMAX)


def dct_matrix() -> F32Arr:
    """The orthonormal DCT that maps 40 log-mel energies to 40 MFCCs."""
    # Built by transforming an identity matrix, so it IS scipy's transform in
    # matrix form -- no hand-derived cosine formula to get a scale factor wrong.
    dct = scipy.fftpack.dct(np.eye(params.N_MELS), axis=0, type=2, norm="ortho")
    return dct[: params.N_MFCC, :].astype(np.float32)


def chroma_filterbank() -> F32Arr:
    """The 40-bin chroma filterbank at the frozen tuning of 0.0."""
    # Exactly the internal librosa.filters.chroma call (defaults: ctroct=5.0,
    # octwidth=2, norm=2, base_c=True); float32 natively, like the mel bank.
    return librosa.filters.chroma(sr=params.SR, n_fft=params.N_FFT,
                                  n_chroma=params.N_CHROMA, tuning=params.TUNING)


def extract_features_ref(raw: extract.BytesLike) -> F32Arr:
    """The table-only feature pipeline: same (40, 3) as extract_features.

    Every step uses only the frozen tables and operations the chip can do
    (multiply, real FFT, matrix-vector products, log10, max, mean) -- no
    librosa at runtime. Math is float64 here for a clean reference; the chip
    runs the identical operation order in float32, and the parity tolerance
    absorbs that precision gap. Buffering differs by design (the chip builds
    frames one at a time instead of materializing the padded signal); the
    per-operation arithmetic is the contract, not the buffer strategy.
    """
    y = extract.bytes_to_signal(raw)
    if y.size != params.WINDOW_BYTES:
        raise ValueError(
            f"expected the {params.WINDOW_BYTES}-byte NV window, got {y.size} bytes")

    win = hann_window().astype(np.float64)
    mel_b = mel_filterbank().astype(np.float64)
    dct_m = dct_matrix().astype(np.float64)
    chroma_b = chroma_filterbank().astype(np.float64)

    padded = np.zeros(params.WINDOW_BYTES + 2 * PAD, dtype=np.float64)
    padded[PAD:PAD + params.WINDOW_BYTES] = y

    # 33 windowed 512-point real FFTs -> power spectrum (re^2 + im^2; the
    # abs-then-square librosa writes differs only in last-ulp rounding).
    power = np.empty((N_SPEC_BINS, N_FRAMES), dtype=np.float64)
    for t in range(N_FRAMES):
        start = t * params.HOP_LENGTH
        spec = np.fft.rfft(padded[start:start + params.N_FFT] * win)
        power[:, t] = spec.real ** 2 + spec.imag ** 2

    mel_power = mel_b @ power                                   # (40, 33)

    # MFCC = DCT of the dB mel spectrogram. GOTCHA: the TOP_DB clamp needs the
    # max over ALL 40x33 dB values -- the whole matrix must exist (or the max
    # be found in a first pass) before any value is clamped.
    mel_db = 10.0 * np.log10(np.maximum(AMIN, mel_power))
    mel_db = np.maximum(mel_db, mel_db.max() - TOP_DB)
    mfcc = dct_m @ mel_db                                       # (40, 33)

    # Chroma: bank product, then each FRAME divided by its own peak (values
    # are non-negative, so peak == max |value|); a sub-tiny peak divides by 1.
    chroma_raw = chroma_b @ power                               # (40, 33)
    peaks = chroma_raw.max(axis=0)
    chroma = chroma_raw / np.where(peaks < NORM_TINY, 1.0, peaks)

    vectors = {
        "mfcc": mfcc.mean(axis=1),
        "mel": mel_power.mean(axis=1),
        "chroma_stft": chroma.mean(axis=1),
    }
    feats = np.stack([vectors[name] for name in params.FEATURE_ORDER], axis=1)
    return feats.astype(np.float32)
