"""Score capture files with a mars_v2 model.

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


def score_files(paths: list[Path], model_path: Path, thr: float) -> list[dict]:
    import keras
    m = keras.models.load_model(model_path, compile=False)
    g = np.stack([nv_grid_v2(p.read_bytes()) for p in paths])[..., None].astype(np.float32)
    a = np.stack([nv_struct_features(p.read_bytes()) for p in paths]).astype(np.float32)
    p_anom = m.predict([g, a], verbose=0)[:, 1]
    return [{"file": f.name, "p_anom": round(float(p), 6),
             "verdict": "ANOMALY" if p > thr else "benign"}
            for f, p in zip(paths, p_anom)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--model", type=Path, default=None)
    ap.add_argument("--thr", type=float, default=None)
    args = ap.parse_args()
    model_path, thr = args.model, args.thr
    if (model_path is None or thr is None) and META_JSON.exists():
        meta = json.loads(META_JSON.read_text(encoding="utf-8"))
        model_path = model_path or META_JSON.parent / meta["model"]
        thr = thr if thr is not None else float(meta["threshold"])
    if model_path is None or thr is None:
        raise SystemExit("no packaged weights yet: pass --model and --thr "
                         "(e.g. the frozen exam winner + its frozen.json threshold)")
    for row in score_files(args.files, model_path, thr):
        print(f"{row['file']:50s} p={row['p_anom']:.4f}  {row['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
