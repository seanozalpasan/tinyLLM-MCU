"""Prove the ported encoding matches the graded original, byte for byte.

The ONE place in the package allowed to import exam/AE modules -- it exists
precisely to compare against them. Run after any change to grid.py or
features.py:

    python -m offdevice.mars_v2.parity_check [n_files]
"""
from __future__ import annotations

import sys

import numpy as np

from .features import nv_struct_features
from .grid import nv_grid_v2
from .splits import trainable_files


def main(n: int = 10) -> int:
    from offdevice.detect_v2.grid_v2 import nv_grid_v2 as grid_orig
    from offdevice.cnn_quant.features.nv_struct import nv_struct_features as feat_orig

    files = trainable_files()[:n]
    if not files:
        raise SystemExit("no captures found -- check paths.py")
    bad = 0
    for f in files:
        d = f.read_bytes()
        if not np.array_equal(nv_grid_v2(d), grid_orig(d)):
            print(f"GRID MISMATCH     {f.name}")
            bad += 1
        if not np.array_equal(nv_struct_features(d), feat_orig(d)):
            print(f"FEATURES MISMATCH {f.name}")
            bad += 1
    print(f"parity: {len(files)} files checked, {bad} mismatches "
          f"-> {'FAIL' if bad else 'PASS'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 10))
