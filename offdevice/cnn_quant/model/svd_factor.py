"""
SVD-factor the CNN's fully-connected head: each big Dense(in,out) becomes a
rank-r bottleneck  x@W+b = x @ (U_r sqrt(S_r)) @ (sqrt(S_r) V_r^T) + b,
initialized from the trained W's own truncated SVD. Convs untouched.

Prints int8 size + retained energy per rank; it does NOT gate. Pick r against
held-out task metrics -- energy alone says nothing about verdicts.
"""
import numpy as np
import keras
from keras import layers

from pathlib import Path
from offdevice.cnn_quant.model.quantize import convert_int8
from offdevice.cnn_quant.model.dataset import build_dataset
from offdevice.cnn_quant.model.z_normal import apply_normalizer
from offdevice.cnn_quant.features import params
from offdevice.cnn_quant.paths import CSV, BINS

HERE = Path(__file__).resolve().parent


def svd_pair(W, r):
    """W (in,out) -> A (in,r), B (r,out); A@B is the rank-r best approx.
    Also returns 'energy': fraction of variance the top-r singular values keep."""
    U, S, Vt = np.linalg.svd(W, full_matrices=False)
    A = (U[:, :r] * np.sqrt(S[:r])).astype(np.float32)
    B = (np.sqrt(S[:r])[:, None] * Vt[:r, :]).astype(np.float32)
    energy = float((S[:r] ** 2).sum() / (S ** 2).sum())
    return A, B, energy

def factor_model(base, r):
    """Rebuild `base` with its two big Dense layers each replaced by a rank-r
    A->B pair. Conv/pool/flatten copied verbatim; dropout dropped (no-op at inference)."""
    dense = [l for l in base.layers if isinstance(l, layers.Dense)]
    if len(dense) != 3:
        raise ValueError(f"expected 3 Dense layers (256/512/2), found {len(dense)}")
    d256, d512, d2 = dense

    model = keras.Sequential()
    model.add(layers.Input(base.input_shape[1:]))
    for l in base.layers:
        if isinstance(l, layers.Conv2D):
            model.add(layers.Conv2D.from_config(l.get_config()))
            model.layers[-1].set_weights(l.get_weights())
        elif isinstance(l, layers.MaxPooling2D):
            model.add(layers.MaxPooling2D.from_config(l.get_config()))
        elif isinstance(l, layers.Flatten):
            model.add(layers.Flatten())

    W1, b1 = d256.get_weights()
    W2, b2 = d512.get_weights()
    A1, B1, e1 = svd_pair(W1, r)
    A2, B2, e2 = svd_pair(W2, r)

    model.add(layers.Dense(r, use_bias=False))                  # A1
    model.add(layers.Dense(d256.units, activation="relu"))      # B1
    model.add(layers.Dense(r, use_bias=False))                  # A2
    model.add(layers.Dense(d512.units, activation="relu"))      # B2
    model.add(layers.Dense(d2.units, activation="sigmoid"))     # unchanged

    model.layers[-5].set_weights([A1])
    model.layers[-4].set_weights([B1, b1])
    model.layers[-3].set_weights([A2])
    model.layers[-2].set_weights([B2, b2])
    model.layers[-1].set_weights(d2.get_weights())
    return model, e1, e2

if __name__ == "__main__":

    x_train, _, _, _ = build_dataset(CSV, BINS)
    stats = np.load(HERE / "norm_stats.npz")
    mu, sd = stats["mu"], stats["sd"]
    x_train = apply_normalizer(x_train, mu, sd)   # calibrate on the same distribution the model trained on
    base = keras.models.load_model(
        HERE / f"mars_cnn_{params.ACTIVE_MODE.lower()}.keras", compile=False)
    for r in (16, 32, 64, 128):
        fm, e1, e2 = factor_model(base, r)
        size = len(convert_int8(fm, x_train))         # pass calibration data
        print(f"r={r:3d}  params={fm.count_params():>8,}  int8={size/1024:6.1f} KB  energy={e1:.3f}/{e2:.3f}")