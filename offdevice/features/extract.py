"""
Off-device feature extraction: the 4 KB NV region's bytes -> (40, 3) feature matrix.

Pipeline (see params.py for the frozen values):
    4096 bytes -> uint8 -> int16 -> /32768 float32
              -> MFCC / mel / chroma_stft at SR=22050
              -> time-average each to a 40-vector
              -> column-stack in FEATURE_ORDER -> (40, 3)

Input is exactly the 4 KB NV window (offdevice.nv.parse.slice_nv cuts it out of a
256 KB capture; the CLI below does that automatically). The on-chip CMSIS-DSP port
must produce matching numbers for the same bytes.
"""

from pathlib import Path

import numpy as np
import numpy.typing as npt
import librosa

from offdevice.features import params

BytesLike = bytes | bytearray | memoryview | npt.NDArray[np.uint8]
Source = BytesLike | str | Path

# float32 signal / feature arrays. numpy has no real shape typing, so the shape
# axis stays Any; the dtype is pinned to float32.
Signal = npt.NDArray[np.float32]


def load_dump(path: str | Path) -> bytes:
    """Read a raw .bin memory dump as bytes."""
    return Path(path).read_bytes()


def bytes_to_signal(raw: BytesLike) -> Signal:
    """Widen raw dump bytes to the float32 signal the feature functions expect.

    Each byte as UNSIGNED 0..255 -> int16 (no sign-extend, no centering) -> /32768.
    Guards fail loudly on garbage input: a non-uint8 ndarray would be reinterpreted
    as raw little-endian bytes (garbling samples); an empty dump crashes librosa.
    """
    if isinstance(raw, np.ndarray):
        if raw.dtype != params.BYTE_DTYPE:
            raise TypeError(
                f"raw ndarray must be {params.BYTE_DTYPE.__name__}, got {raw.dtype}"
            )
        buf = raw.tobytes()
    else:
        buf = bytes(raw)  # bytes / bytearray / memoryview
    if len(buf) == 0:
        raise ValueError("empty dump: need at least 1 byte to extract features")

    arr = np.frombuffer(buf, dtype=params.BYTE_DTYPE)
    widened = arr.astype(params.WIDEN_DTYPE)
    return widened.astype(params.SIGNAL_DTYPE) / params.SCALE


def extract_features(source: Source) -> Signal:
    """The NV window's bytes (or a path to a 4 KB .bin) -> deterministic (40, 3) float32.

    Columns follow params.FEATURE_ORDER: [mfcc, mel, chroma_stft]. Rejects any
    input that isn't exactly the 4 KB window — a whole 256 KB capture here is a
    caller bug (slice the NV region first), not a bigger window.
    """
    if isinstance(source, (str, Path)):
        source = load_dump(source)
    y = bytes_to_signal(source)
    if y.size != params.WINDOW_BYTES:
        raise ValueError(
            f"expected the {params.WINDOW_BYTES}-byte NV window, got {y.size} bytes"
        )

    mfcc = librosa.feature.mfcc(
        y=y, sr=params.SR, n_mfcc=params.N_MFCC,
        n_fft=params.N_FFT, hop_length=params.HOP_LENGTH,
        n_mels=params.MFCC_INTERNAL_N_MELS, fmax=params.MFCC_FMAX,
    )
    mel = librosa.feature.melspectrogram(
        y=y, sr=params.SR, n_mels=params.N_MELS, fmax=params.FMAX,
        n_fft=params.N_FFT, hop_length=params.HOP_LENGTH,
    )
    chroma = librosa.feature.chroma_stft(
        y=y, sr=params.SR, n_chroma=params.N_CHROMA,
        n_fft=params.N_FFT, hop_length=params.HOP_LENGTH,
        tuning=params.TUNING,
    )

    # Time-average each (n_bins, n_frames) feature to a (n_bins,) vector.
    vectors = {
        "mfcc": np.mean(mfcc, axis=params.TIME_AXIS),
        "mel": np.mean(mel, axis=params.TIME_AXIS),
        "chroma_stft": np.mean(chroma, axis=params.TIME_AXIS),
    }

    # Column-stack in the frozen feature order -> (40, 3).
    feats = np.stack([vectors[name] for name in params.FEATURE_ORDER], axis=1)
    return feats.astype(params.SIGNAL_DTYPE)


def feature_stats(feats: Signal) -> dict[str, dict[str, float]]:
    """Per-feature (per-column) min/max -- grounds later quantization."""
    return {
        name: {"min": float(feats[:, j].min()), "max": float(feats[:, j].max())}
        for j, name in enumerate(params.FEATURE_ORDER)
    }


if __name__ == "__main__":
    import sys

    from offdevice.nv.parse import slice_nv

    if len(sys.argv) != 2:
        print("usage: python -m offdevice.features.extract <capture.bin | nv-slice.bin>")
        raise SystemExit(2)

    data = load_dump(sys.argv[1])
    if len(data) != params.WINDOW_BYTES:
        data = slice_nv(data)   # a full 256 KB capture: cut out the NV region
    feats = extract_features(data)
    print(f"shape={feats.shape} dtype={feats.dtype}")
    for name, s in feature_stats(feats).items():
        print(f"  {name:12s} min={s['min']:+.6e} max={s['max']:+.6e}")
