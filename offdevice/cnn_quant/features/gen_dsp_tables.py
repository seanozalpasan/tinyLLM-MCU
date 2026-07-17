### IDK if this is needed rn 
"""
Generate mars_dsp_tables.h -- every numerically-opinionated constant the
on-chip frontend needs, produced by the SAME librosa install that trained
the model (window shape, mel scale + norm, DCT norm all inherit librosa's
defaults instead of being re-derived, wrongly, in C).

    hann[512]          periodic Hann (fftbins=True)
    mel_fb[40*257]     Slaney-scale, slaney-normalized mel filterbank
    chroma_fb[40*257]  chroma_stft filterbank, tuning=0
    dct[40*40]         orthonormal DCT-II  (mfcc = DCT @ log_mel)

Then runs gate P0 (DESIGN.md): re-implements the whole OPTIMIZED feature
chain in NumPy using ONLY the emitted tables + zero-padded center framing,
and asserts it matches librosa.feature.mfcc / melspectrogram / chroma_stft
on a seeded random signal. If P0 fails, the header is still written but
marked untrusted -- fix the mismatch (pad_mode? librosa version?) first.

Tables are DENSE (~91 KB of flash) -- fine for host-side parity work, must be
sparsified before flash integration (DESIGN.md has the budget). Nonzero
stats are printed so the sparse payoff is visible.

NOTE: feeds the constant-predictor lane -- parity here is an engineering
number, not detection.
"""
import sys

import numpy as np
import librosa
import scipy.signal
import scipy.fft

from pathlib import Path
from offdevice.cnn_quant.features import params

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[2] / "firmware" / "CNN memAcq" / "mfcc" / "mars_dsp_tables.h"

# On-chip scope is OPTIMIZED only (DESIGN.md) -- window size is pinned here,
# independent of ACTIVE_MODE, so regenerating under BASELINE can't drift it.
WINDOW_BYTES = 49152
N_FRAMES = 1 + WINDOW_BYTES // params.HOP_LENGTH


# ---- tables -------------------------------------------------------------------
def make_tables():
    hann = scipy.signal.get_window("hann", params.N_FFT, fftbins=True)
    mel_fb = librosa.filters.mel(
        sr=params.SR, n_fft=params.N_FFT, n_mels=params.N_MELS, fmax=params.FMAX)
    chroma_fb = librosa.filters.chroma(
        sr=params.SR, n_fft=params.N_FFT, n_chroma=params.N_CHROMA,
        tuning=params.TUNING)
    # dct[:, j] = DCT-II(e_j), so mfcc_vec = dct @ logmel_vec
    dct = scipy.fft.dct(np.eye(params.N_MELS), type=2, norm="ortho", axis=0)
    return (hann.astype(np.float32), mel_fb.astype(np.float32),
            chroma_fb.astype(np.float32), dct.astype(np.float32))


# ---- gate P0: table-only NumPy chain vs librosa ---------------------------------
def numpy_chain(y, hann, mel_fb, chroma_fb, dct):
    """extract.py's OPTIMIZED features using ONLY the tables + explicit
    framing -- the exact algorithm mars_dsp.c implements."""
    half = params.N_FFT // 2
    ypad = np.pad(y, half, mode="constant")      # center=True, zero pad
    frames = np.stack([ypad[f * params.HOP_LENGTH:
                            f * params.HOP_LENGTH + params.N_FFT]
                       for f in range(N_FRAMES)])
    S = np.abs(np.fft.rfft(frames * hann, axis=1)) ** 2     # (n_frames, 257)

    mel = S @ mel_fb.T                                       # (n_frames, 40)
    db = 10.0 * np.log10(np.maximum(mel, 1e-10))
    db = np.maximum(db, db.max() - 80.0)                     # top_db clamp, global max
    mfcc = db.mean(axis=0) @ dct.T                           # avg-then-DCT (linear)

    chroma = S @ chroma_fb.T                                 # (n_frames, 40)
    cmax = chroma.max(axis=1, keepdims=True)
    chroma = chroma / np.where(cmax > np.finfo(np.float32).tiny, cmax, 1.0)

    return mfcc, mel.mean(axis=0), chroma.mean(axis=0)


def self_check(hann, mel_fb, chroma_fb, dct):
    rng = np.random.default_rng(1337)
    y = (rng.integers(0, 256, WINDOW_BYTES).astype(np.int16)
         .astype(np.float32) / 32768.0)                      # the byte->signal contract

    got_mfcc, got_mel, got_chroma = numpy_chain(
        y, hann.astype(np.float64), mel_fb.astype(np.float64),
        chroma_fb.astype(np.float64), dct.astype(np.float64))

    ref_mfcc = librosa.feature.mfcc(
        y=y, sr=params.SR, n_mfcc=params.N_MFCC, n_fft=params.N_FFT,
        hop_length=params.HOP_LENGTH, n_mels=params.MFCC_INTERNAL_N_MELS,
        fmax=params.FMAX).mean(axis=1)
    ref_mel = librosa.feature.melspectrogram(
        y=y, sr=params.SR, n_mels=params.N_MELS, fmax=params.FMAX,
        n_fft=params.N_FFT, hop_length=params.HOP_LENGTH).mean(axis=1)
    ref_chroma = librosa.feature.chroma_stft(
        y=y, sr=params.SR, n_chroma=params.N_CHROMA, n_fft=params.N_FFT,
        hop_length=params.HOP_LENGTH, tuning=params.TUNING).mean(axis=1)

    ok = True
    for name, got, ref, atol in (("mfcc", got_mfcc, ref_mfcc, 1e-6),
                                 ("mel", got_mel, ref_mel, 1e-12),
                                 ("chroma_stft", got_chroma, ref_chroma, 1e-6)):
        diff = np.abs(got - ref).max()
        passed = np.allclose(got, ref, rtol=1e-5, atol=atol)
        ok &= passed
        print(f"  P0 {name:12s} max|diff|={diff:.3e}  {'PASS' if passed else 'FAIL'}")
    if not ok:
        print("  P0 FAILED -- likely a librosa default mismatch (pad_mode changed "
              "across versions). Do NOT trust the header until this passes.")
    return ok


# ---- emission -------------------------------------------------------------------
def c_floats(arr, per_line=6):
    flat = np.asarray(arr, dtype=np.float32).ravel()
    lines = []
    for i in range(0, len(flat), per_line):
        lines.append("  " + " ".join(f"{v:.9e}f," for v in flat[i:i + per_line]))
    return "\n".join(lines)


def nz(a, tol=0.0):
    return int(np.count_nonzero(np.abs(a) > tol))


if __name__ == "__main__":
    hann, mel_fb, chroma_fb, dct = make_tables()

    print(f"librosa {librosa.__version__}, numpy {np.__version__}")
    p0 = self_check(hann, mel_fb, chroma_fb, dct)

    print(f"  mel_fb    nonzero {nz(mel_fb):5d} / {mel_fb.size}  "
          f"({100 * nz(mel_fb) / mel_fb.size:.1f}% -- sparse CSR would be tiny)")
    print(f"  chroma_fb nonzero {nz(chroma_fb):5d} / {chroma_fb.size}  "
          f"({100 * nz(chroma_fb) / chroma_fb.size:.1f}%)")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="\n") as f:
        f.write(f"""// GENERATED by gen_dsp_tables.py - do not edit.
// librosa {librosa.__version__}  numpy {np.__version__}  python {sys.version.split()[0]}
// params: SR={params.SR} N_FFT={params.N_FFT} HOP={params.HOP_LENGTH}
//         N_MELS={params.N_MELS} N_CHROMA={params.N_CHROMA} FMAX={params.FMAX}
//         WINDOW_BYTES={WINDOW_BYTES} N_FRAMES={N_FRAMES}
// gate P0 (table self-check vs librosa): {'PASS' if p0 else 'FAIL -- DO NOT TRUST'}
// DENSE tables (~91 KB) -- host parity work only; sparsify before flash
// integration (DESIGN.md).
#ifndef MARS_DSP_TABLES_H
#define MARS_DSP_TABLES_H

// periodic Hann, scipy get_window("hann", {params.N_FFT}, fftbins=True)
const float mars_dsp_hann[{params.N_FFT}] = {{
{c_floats(hann)}
}};

// librosa.filters.mel (Slaney scale, slaney norm), row-major (filter, bin)
const float mars_dsp_mel_fb[{params.N_MELS} * {mel_fb.shape[1]}] = {{
{c_floats(mel_fb)}
}};

// librosa.filters.chroma (tuning=0), row-major (chroma, bin)
const float mars_dsp_chroma_fb[{params.N_CHROMA} * {chroma_fb.shape[1]}] = {{
{c_floats(chroma_fb)}
}};

// orthonormal DCT-II: mfcc[k] = sum_n dct[k*{params.N_MELS}+n] * log_mel[n]
const float mars_dsp_dct[{params.N_MELS} * {params.N_MELS}] = {{
{c_floats(dct)}
}};

#endif  // MARS_DSP_TABLES_H
""")
    print(f"wrote {OUT}")
    if not p0:
        raise SystemExit(1)
