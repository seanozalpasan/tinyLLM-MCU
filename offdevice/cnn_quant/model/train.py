"""
Train entry point: metadata.csv + bins -> mars_cnn_<mode>.keras + norm_stats.npz.

mu/sd are fit on TRAIN only and every downstream consumer (quantize, svd_factor,
export, the C frontend) applies the SAME stats -- normalize-here-but-not-there is
the silent killer in this pipeline. Retrain => rerun everything downstream.
"""
import numpy as np

from pathlib import Path

from offdevice.cnn_quant.model.dataset import build_dataset
from offdevice.cnn_quant.model.model import build_model, train_model, evaluate_model
from offdevice.cnn_quant.features import params
from offdevice.cnn_quant.model.z_normal import fit_normalizer, identity_stats, apply_normalizer
from offdevice.cnn_quant.paths import CSV, BINS


HERE = Path(__file__).resolve().parent

if __name__ == "__main__":
    x_train, y_train, x_test, y_test = build_dataset(CSV, BINS)

    # NORMALIZE=False (MARS fed raw features) writes identity stats rather than
    # skipping the file, so downstream keeps loading norm_stats.npz unchanged.
    mu, sd = fit_normalizer(x_train) if params.NORMALIZE else identity_stats(x_train)
    x_train = apply_normalizer(x_train, mu, sd)
    x_test = apply_normalizer(x_test, mu, sd)
    np.savez(HERE / "norm_stats.npz", mu=mu, sd=sd)

    print(f"mode={params.ACTIVE_MODE} assembly={params.ASSEMBLY} "
          f"normalize={params.NORMALIZE} train={x_train.shape} test={x_test.shape}")

    model = build_model()
    save_path = HERE / f"mars_cnn_{params.ACTIVE_MODE.lower()}.keras"
    model, history = train_model(model, x_train, y_train, x_test, y_test, save_path=save_path)
    evaluate_model(model, x_test, y_test)
