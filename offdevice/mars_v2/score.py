"""
Score capture files with a mars_v2 model. With no options this uses the
packaged weights + calibrated threshold (weights/mars_v2.json), so it works
out of the box:

    python -m offdevice.mars_v2.score <capture.bin> [...]
        [--model path.keras] [--thr 0.4062]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .features import nv_struct_features
from .grid import nv_grid_v2
from .paths import META_JSON


def score_files(paths: list[Path], model_path: Path,
                threshold: float) -> list[dict]:
    import keras
    model = keras.models.load_model(model_path, compile=False)
    grids = np.stack([nv_grid_v2(path.read_bytes())
                      for path in paths])[..., None].astype(np.float32)
    structural = np.stack([nv_struct_features(path.read_bytes())
                           for path in paths]).astype(np.float32)
    probabilities = model.predict([grids, structural], verbose=0)[:, 1]
    return [{"file": path.name, "p_anom": round(float(probability), 6),
             "verdict": "ANOMALY" if probability > threshold else "benign"}
            for path, probability in zip(paths, probabilities)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--model", type=Path, default=None)
    parser.add_argument("--thr", type=float, default=None)
    args = parser.parse_args()

    model_path = args.model
    threshold = args.thr
    if (model_path is None or threshold is None) and META_JSON.exists():
        meta = json.loads(META_JSON.read_text(encoding="utf-8"))
        model_path = model_path or META_JSON.parent / meta["model"]
        threshold = threshold if threshold is not None else float(meta["threshold"])
    if model_path is None or threshold is None:
        raise SystemExit("no packaged weights found: pass --model and --thr")

    for row in score_files(args.files, model_path, threshold):
        print(f"{row['file']:50s} p={row['p_anom']:.4f}  {row['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
