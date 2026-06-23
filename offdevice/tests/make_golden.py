"""One-time golden-vector generator. RUN BY SEAN, not Claude Code.

Generates the (40, 3) feature matrix for the synthetic fixture and writes it to
offdevice/tests/golden/synthetic_features.npy. Run once, eyeball the printed
stats for sanity, then COMMIT the .npy. test_features.py guards it thereafter.

    python -m offdevice.tests.make_golden        # run from the repo root

Re-run ONLY when a feature change is intended (then re-freeze deliberately and
note it in DATASET.md).
"""

from pathlib import Path

import numpy as np

from offdevice.features import extract
from offdevice.tests.fixtures import synthetic_dump

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
GOLDEN_PATH = GOLDEN_DIR / "synthetic_features.npy"


def main() -> None:
    feats = extract.extract_features(synthetic_dump())
    GOLDEN_DIR.mkdir(exist_ok=True)
    np.save(GOLDEN_PATH, feats)
    print(f"wrote {GOLDEN_PATH}")
    print(f"shape={feats.shape} dtype={feats.dtype}")
    for name, s in extract.feature_stats(feats).items():
        print(f"  {name:12s} min={s['min']:+.6e} max={s['max']:+.6e}")


if __name__ == "__main__":
    main()
