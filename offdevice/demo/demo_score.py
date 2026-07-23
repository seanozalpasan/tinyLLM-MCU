"""
Poster-demo scorer: the chip's exact anomaly math, run live on the laptop.

The audience-facing "watch it catch a real attack" moment. With no arguments it
scores the demo twin pair -- a committed benign board capture and the
collaborator's attacked copy of that same capture (a real 1024-byte foreign
payload hidden in the NV region) -- and prints a plain-English verdict box for
each. Any capture path(s) can be scored
instead. Read-only: nothing here touches the board, the model artifact, or the
dataset.

The math is not a reimplementation: scoring goes through the same
load_model/score_bytes path as the eval harness, which is host-parity-proven
against the C scorer the chip runs. Same bytes in, same number out.

    python -m offdevice.demo.demo_score                 # the poster twin pair
    python -m offdevice.demo.demo_score <capture.bin> [...]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from offdevice.features import params
from offdevice.model.fit import MahalanobisModel, load_model
from offdevice.model.score import score_bytes
from offdevice.nv.parse import DUMP_SIZE, slice_nv, summarize

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT = REPO_ROOT / "offdevice" / "model" / "artifacts" / "mahalanobis.npz"

# The twin pair: same board capture, except the attacked copy carries a hidden
# 1024 B payload (a collaborator-crafted anomaly, not one we generated). The
# benign twin is pinned holdout -- the model never trained on it.
DEMO_BENIGN = (REPO_ROOT / "offdevice" / "data" / "captures"
               / "benign__tbA__nv15s-lab-steady1__run014__20260712T075344.bin")
DEMO_ATTACKED = (REPO_ROOT / "offdevice" / "data" / "anomalies"
                 / "anom_obvious__steady1_run014__blob1024.bin")

RULE = "=" * 74


def _describe_region(nv: bytes) -> str:
    """Parser's-eye view of the 4 KB region, indented; tampered bytes may make
    it partially unreadable -- that is itself worth showing, so never crash."""
    try:
        text = summarize(nv)
    except Exception as e:  # tampered structure: report, don't die mid-demo
        return f"    (structure not fully parseable: {e})"
    return "\n".join("    " + line for line in text.splitlines())


def _verdict_box(d: float, threshold: float) -> str:
    """The audience-readable verdict: one bordered block, margin spelled out."""
    anomaly = d > threshold
    verdict = "ANOMALY" if anomaly else "BENIGN"
    margin = d - threshold
    detail = (f"{margin:+.3f} OVER the alarm line -- watchdog kick withheld"
              if anomaly else
              f"{margin:+.3f} vs the alarm line -- device keeps running")
    lines = [
        f"VERDICT: {verdict}",
        f"distance {d:.3f}   alarm line {threshold:.3f}",
        detail,
    ]
    width = max(len(s) for s in lines) + 4
    top = "+" + "-" * width + "+"
    body = "\n".join(f"|  {s:<{width - 2}}|" for s in lines)
    return f"{top}\n{body}\n{top}"


def score_one(model: MahalanobisModel, path: Path, role: str | None) -> float:
    """Score one capture with the narrated stage-by-stage story."""
    raw = path.read_bytes()
    if len(raw) not in (params.WINDOW_BYTES, DUMP_SIZE):
        raise SystemExit(f"{path.name}: {len(raw)} bytes -- expected a "
                         f"{DUMP_SIZE}-byte whole-flash capture or a bare "
                         f"{params.WINDOW_BYTES}-byte NV image")

    print(RULE)
    print(f"  FILE: {path.name}")
    if role:
        print(f"        ({role})")
    print(RULE)

    if len(raw) == DUMP_SIZE:
        nv = slice_nv(raw)
        print(f"  1. Whole-flash capture ({DUMP_SIZE // 1024} KB) -> cut out the "
              f"4 KB region the sensor logger writes.")
    else:
        nv = raw
        print("  1. Bare 4 KB NV-region image -- scored as-is.")

    print("  2. What a parser sees in those 4096 bytes:")
    print(_describe_region(nv))

    print("  3. Turn the bytes into the model's 120 texture measurements,")
    print("     compute the learned distance from normal (Mahalanobis).")
    d = score_bytes(model, raw)
    print()
    print(_verdict_box(d, model.threshold))
    print()
    return d


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Poster demo: score captures with the chip's exact math.")
    ap.add_argument("paths", nargs="*", type=Path,
                    help="captures to score (none = the demo twin pair)")
    ap.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT,
                    help="fitted model artifact (default: the deployed model)")
    args = ap.parse_args()

    model = load_model(args.artifact)
    if model.threshold is None:
        raise SystemExit("artifact has no threshold -- not the deployed model")

    if not args.paths:
        pairs = [(DEMO_BENIGN, "the board's own data, captured mid-operation; "
                               "the model NEVER trained on this file"),
                 (DEMO_ATTACKED, "the SAME capture, attacked: 1024 hidden bytes "
                                 "of foreign payload in the data region")]
        missing = [p for p, _ in pairs if not p.exists()]
    else:
        pairs = [(p, None) for p in args.paths]
        missing = [p for p, _ in pairs if not p.exists()]
    if missing:
        raise SystemExit("missing file(s): " + ", ".join(str(p) for p in missing))

    print()
    for path, role in pairs:
        score_one(model, path, role)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
