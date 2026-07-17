"""
Dump bytes -> the CNN's (n_windows, 40, N) feature tensor.

.bin -> uint8 -> int16 -> /32768 float32 -> per WINDOW_BYTES slice: resample
48k -> 22.05k, librosa mfcc/mel/chroma time-averaged to 40-vectors, assembled
per params.ASSEMBLY.

Windowing is byte-domain and the resample runs per-window, after the slice:
resampling first breaks the 1-byte-1-sample identity WINDOW_BYTES relies on
(262144 bytes -> 120423 samples -> zero windows).

All parameters are in params.py.
"""

from pathlib import Path

import numpy.typing as npt
import numpy as np
import librosa
import sys

from offdevice.cnn_quant.features import params

BytesLike = bytes | bytearray | memoryview | npt.NDArray[np.uint8]
Source = BytesLike | str | Path

Signal = npt.NDArray[np.float32]


def load_dump(path: str | Path) -> bytes:
    """Read a raw .bin memory dump as bytes."""
    return Path(path).read_bytes()


def bytes_to_signal(raw: BytesLike) -> Signal:
    """Dump bytes -> float32 signal: each byte UNSIGNED 0..255 -> int16 -> /32768."""
    if isinstance(raw, np.ndarray):
        # a non-uint8 array would be reinterpreted as little-endian bytes
        if raw.dtype != params.BYTE_DTYPE:
            raise TypeError(
                f"raw ndarray must be {params.BYTE_DTYPE.__name__}, got {raw.dtype}"
            )
        buf = raw.tobytes()
    else:
        buf = bytes(raw)
    if len(buf) == 0:
        raise ValueError("empty dump: need at least 1 byte to extract features")

    arr = np.frombuffer(buf, dtype=params.BYTE_DTYPE)
    widened = arr.astype(params.WIDEN_DTYPE)
    return widened.astype(params.SIGNAL_DTYPE) / params.SCALE


# One thunk per family so window_features only pays for what's in FEATURE_ORDER;
# chroma_cqt/cens dominate runtime and OPTIMIZED drops them.
def _feature_fns(y: Signal) -> dict:
    return {
        # no fmax: MARS passed none to mfcc, so its mel bank runs to Nyquist
        "mfcc": lambda: librosa.feature.mfcc(
            y=y, sr=params.SR, n_mfcc=params.N_MFCC,
            n_fft=params.N_FFT, hop_length=params.HOP_LENGTH,
            n_mels=params.MFCC_INTERNAL_N_MELS,
        ),
        "mel": lambda: librosa.feature.melspectrogram(
            y=y, sr=params.SR, n_mels=params.N_MELS, fmax=params.FMAX,
            n_fft=params.N_FFT, hop_length=params.HOP_LENGTH,
        ),
        "chroma_stft": lambda: librosa.feature.chroma_stft(
            y=y, sr=params.SR, n_chroma=params.N_CHROMA,
            n_fft=params.N_FFT, hop_length=params.HOP_LENGTH,
            tuning=params.TUNING,
        ),
        "chroma_cqt": lambda: librosa.feature.chroma_cqt(
            y=y, sr=params.SR, n_chroma=params.N_CHROMA,
            bins_per_octave=params.BINS_PER_OCTAVE, hop_length=params.HOP_LENGTH,
            tuning=params.TUNING,
        ),
        "chroma_cens": lambda: librosa.feature.chroma_cens(
            y=y, sr=params.SR, n_chroma=params.N_CHROMA,
            bins_per_octave=params.BINS_PER_OCTAVE, hop_length=params.HOP_LENGTH,
            tuning=params.TUNING,
        ),
    }


def resample_signal(y: Signal) -> Signal:
    """The 48k -> 22.05k step MARS got from librosa.load; bit-identical without the wav."""
    if not params.MARS_WAV_RESAMPLE:
        return y
    return librosa.resample(y, orig_sr=params.WAV_SR, target_sr=params.SR,
                            res_type=params.RES_TYPE)


def window_features(y: Signal) -> Signal:
    """One window's signal -> the (40, N) CNN input image, per params.ASSEMBLY."""
    fns = _feature_fns(y)
    vecs = [np.mean(fns[name](), axis=params.TIME_AXIS) for name in params.FEATURE_ORDER]

    if params.ASSEMBLY == "mars_reshape":
        return np.reshape(np.vstack(vecs), params.FEATURE_SHAPE)
    return np.stack(vecs, axis=1)


def iter_windows(y: Signal):
    """Yield each WINDOW_BYTES slice, stepping by WINDOW_STRIDE; partials dropped."""
    w, s = params.WINDOW_BYTES, params.WINDOW_STRIDE
    for start in range(0, y.size - w + 1, s):
        yield y[start:start + w]


def extract_features(source: Source) -> Signal:
    """Dump bytes/path -> (n_windows, 40, N) float32. BASELINE: n_windows == 1."""
    if isinstance(source, (str, Path)):
        source = load_dump(source)
    y = bytes_to_signal(source)
    windows = [window_features(resample_signal(w)) for w in iter_windows(y)]
    if not windows:
        raise ValueError(
            f"signal has {y.size} bytes, shorter than one {params.WINDOW_BYTES}-byte window"
        )
    return np.stack(windows, axis=0).astype(params.SIGNAL_DTYPE)


def column_stats(feats: Signal) -> dict[str, dict[str, float]]:
    """Per-column min/max of the assembled image -- grounds later quantization.

    Columns are features only under ASSEMBLY="stack"; mars_reshape mixes all
    five families into every column, so those are labelled by index.
    """
    per_feature = params.ASSEMBLY == "stack"
    labels = params.FEATURE_ORDER if per_feature else [f"col{j}" for j in range(params.N_FEATURES)]
    return {
        name: {"min": float(feats[:, :, j].min()), "max": float(feats[:, :, j].max())}
        for j, name in enumerate(labels)
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m offdevice.cnn_quant.features.extract <capture.bin>")
        raise SystemExit(2)

    feats = extract_features(load_dump(sys.argv[1]))
    print(f"mode={params.ACTIVE_MODE} assembly={params.ASSEMBLY} "
          f"resample={params.MARS_WAV_RESAMPLE}")
    print(f"shape={feats.shape} dtype={feats.dtype}")
    for name, s in column_stats(feats).items():
        print(f"  {name:12s} min={s['min']:+.6e} max={s['max']:+.6e}")
