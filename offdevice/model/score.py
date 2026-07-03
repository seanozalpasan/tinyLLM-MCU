"""
Score captures against a saved Mahalanobis artifact -- the honest-exam runner.

Two uses: (1) the ONE-shot holdout check after a threshold is agreed -- fit.py never
reads holdout bytes, so this is the only place the exam set is graded (grade it once;
repeated peeking while adjusting the threshold turns the exam into more training
data); and (2) ad-hoc scoring of any capture during eval or debugging. Prints one
line per file: distance, threshold, verdict. Holdout runs are cross-checked against
the artifact: the list must be the one the fit excluded, and none of its names may
be quarantined -- otherwise the false-positive number the exam produces is not the
model's.

    python -m offdevice.model.score offdevice\\model\\artifacts\\mahalanobis.npz --holdout offdevice\\data\\holdout.txt
    python -m offdevice.model.score offdevice\\model\\artifacts\\mahalanobis.npz <capture.bin> [...]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from offdevice.data.capture import DEFAULT_CAPTURES_DIR
from offdevice.features import params
from offdevice.features.extract import extract_features
from offdevice.model.dataset import DEFAULT_QUARANTINE, flatten_features, read_quarantine
from offdevice.model.fit import MahalanobisModel, distances, load_model
from offdevice.model.split import read_holdout
from offdevice.nv.parse import DUMP_SIZE, slice_nv


def score_bytes(model: MahalanobisModel, data: bytes) -> float:
    """Distance of one capture (a 256 KB dump or a bare 4 KB NV slice)."""
    nv = data if len(data) == params.WINDOW_BYTES else slice_nv(data)
    x = flatten_features(extract_features(nv)).astype(np.float64)
    return float(distances(model.mean, model.precision, x)[0])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Score captures against a saved Mahalanobis artifact.")
    ap.add_argument("artifact", type=Path, help="the .npz written by offdevice.model.fit")
    ap.add_argument("paths", nargs="*", type=Path, help="capture .bin files to score")
    ap.add_argument("--holdout", type=Path, default=None,
                    help="score every file named in this holdout .txt instead")
    ap.add_argument("--captures-dir", type=Path, default=DEFAULT_CAPTURES_DIR,
                    help="where holdout names resolve (default the captures dir)")
    args = ap.parse_args()

    if bool(args.paths) == (args.holdout is not None):
        print("give capture paths OR --holdout, not both/neither")
        return 2

    model = load_model(args.artifact)
    if model.threshold is None:
        print("[score] artifact has NO threshold yet -- distances only, no verdicts")

    if args.holdout is not None:
        # The exam is only honest against the exact list the fit excluded: a
        # --no-holdout artifact trained on these files, and a different list
        # grades captures the model may have seen.
        recorded = model.meta.get("holdout_file")
        if recorded is None:
            print("[score] artifact was fitted with --no-holdout (plumbing) -- these "
                  "files may be its training data; re-fit with the holdout excluded")
            return 1
        if Path(str(recorded)).resolve() != args.holdout.resolve():
            print(f"[score] --holdout {args.holdout} is not the list the artifact "
                  f"excluded ({recorded}) -- grading a different exam invalidates "
                  f"the false-positive check")
            return 1
        names = read_holdout(args.holdout)
        quarantined = sorted(names & read_quarantine())
        if quarantined:
            print(f"[score] {len(quarantined)} holdout name(s) are quarantined "
                  f"({DEFAULT_QUARANTINE.name}) -- a retracted capture can't grade "
                  f"the exam; re-run split: {quarantined[:4]}")
            return 1
        paths = sorted(args.captures_dir / name for name in names)
    else:
        paths = args.paths
    if not paths:
        print(f"[score] nothing to score -- {args.holdout} names no files")
        return 1
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"[score] {len(missing)} file(s) not found (holdout list out of sync with "
              f"{args.captures_dir}?): {[p.name for p in missing[:4]]}")
        return 1

    flagged = 0
    for path in paths:
        raw = path.read_bytes()
        if len(raw) not in (params.WINDOW_BYTES, DUMP_SIZE):
            print(f"[score] {path.name}: {len(raw)} bytes -- expected a {DUMP_SIZE}-byte "
                  f"capture or a {params.WINDOW_BYTES}-byte NV slice (truncated file?)")
            return 1
        d = score_bytes(model, raw)
        if model.threshold is None:
            print(f"{path.name:60s} d={d:9.3f}")
        else:
            anomaly = d > model.threshold
            flagged += anomaly
            print(f"{path.name:60s} d={d:9.3f} thr={model.threshold:.3f} "
                  f"{'ANOMALY' if anomaly else 'benign'}")
    if model.threshold is not None:
        print(f"\n{flagged} of {len(paths)} flagged "
              f"({flagged / len(paths):.0%} -- on holdout this is the false-positive check)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
