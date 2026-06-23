"""
Off-device feature extraction: raw memory-dump bytes -> (40, 3) feature matrix.

Clean contract (see params.py): no WAV round-trip, no 48 kHz write, no resample.
    bytes -> uint8 -> int16 -> /32768.0 float32
          -> MFCC / melspectrogram / chroma_stft at SR=22050
          -> time-average each to a 40-vector
          -> clean column-stack in FEATURE_ORDER -> (40, 3)

This is the off-device twin of the Week 6 on-chip CMSIS-DSP path; the two MUST
produce matching numbers for the same input bytes.
"""

from pathlib import Path

import numpy as np
import numpy.typing as npt
import librosa

from offdevice.features import params

BytesLike = bytes | bytearray | memoryview | npt.NDArray[np.uint8]
Source = BytesLike | str | Path

# The float32 signal / feature arrays this module produces. numpy has no real
# shape typing yet, so the shape axis stays Any; the dtype is pinned to float32.
Signal = npt.NDArray[np.float32]


def load_dump(path: str | Path) -> bytes:
    """Read a raw .bin memory dump as bytes."""
    return Path(path).read_bytes()


def bytes_to_signal(raw: BytesLike) -> Signal:
    """Widen raw dump bytes to the float32 signal the feature functions expect.

    Each byte is treated as UNSIGNED 0..255, widened to int16 (no sign
    extension, no centering), then divided by 32768.0. Mirrors the original
    ``np.array(bytearray(...), dtype=np.int16)`` followed by 16-bit PCM
    normalization, but without ever touching a WAV file.

    Guards (so a wrong input fails loudly instead of producing silent garbage):
    a non-uint8 ndarray would otherwise be reinterpreted as raw little-endian
    bytes (doubling/garbling the samples); an empty dump would crash librosa
    with an opaque error downstream.
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
    """Raw dump bytes (or a path to a .bin) -> deterministic (40, 3) float32.

    Columns follow params.FEATURE_ORDER: [mfcc, mel, chroma_stft].
    """
    if isinstance(source, (str, Path)):
        source = load_dump(source)
    y = bytes_to_signal(source)

    mfcc = librosa.feature.mfcc(
        y=y, sr=params.SR, n_mfcc=params.N_MFCC,
        n_fft=params.N_FFT, hop_length=params.HOP_LENGTH,
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

    # Clean column-stack in the FROZEN feature order -> (40, 3).
    feats = np.stack([vectors[name] for name in params.FEATURE_ORDER], axis=1)
    return feats.astype(params.SIGNAL_DTYPE)


def feature_stats(feats: Signal) -> dict[str, dict[str, float]]:
    """Per-feature (per-column) min/max -- grounds Week 3 quantization."""
    return {
        name: {"min": float(feats[:, j].min()), "max": float(feats[:, j].max())}
        for j, name in enumerate(params.FEATURE_ORDER)
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m offdevice.features.extract <dump.bin>")
        raise SystemExit(2)

    feats = extract_features(sys.argv[1])
    print(f"shape={feats.shape} dtype={feats.dtype}")
    for name, s in feature_stats(feats).items():
        print(f"  {name:12s} min={s['min']:+.6e} max={s['max']:+.6e}")
