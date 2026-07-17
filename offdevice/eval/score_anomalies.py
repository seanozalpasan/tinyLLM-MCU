"""
Score a verified anomaly delivery against the shipped model -- the eval proper.

Consumes the manifest that offdevice/eval/intake.py derived (run intake first;
nothing is scored that didn't pass it) and the committed fit artifact. Every file
travels the SAME bytes -> features -> distance path as benign data -- no benign
structure gates are applied here, deliberately: an anomaly is allowed to be
structurally broken, that is the point of it. Each anomaly is reported NEXT TO its
own base's benign score, so a detection always reads "the tamper moved this file
from X to Y" and a base that alarms untampered can never pad the numbers.

Anomaly types split into three reporting buckets that are never mixed:
  headline      -- the detection claim (per-type catch rates, ROC/AUC, blob sweep)
  floor         -- journal tampers: measures the detection floor, not a headline
  designed_miss -- classes the detector is NOT supposed to catch; expectations only

The benign side of every false-alarm number is the artifact's 122 leave-one-out
distances (each training capture scored by a model fitted without it) -- the same
distribution the threshold came from, and the only honest benign scores that exist
without new data. The spent 31-capture holdout is never rescored here; the one
anchor base below reproduces an already-revealed number, it does not grade anything.

Outputs: console tables, results JSON, and three PNGs (score line, blob sweep,
ROC appendix) under offdevice/eval/results/.

    python -m offdevice.eval.score_anomalies
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from offdevice.data.capture import DEFAULT_CAPTURES_DIR
from offdevice.eval import report_plots
from offdevice.eval.intake import DEFAULT_ANOMALIES_DIR, MANIFEST_NAME
from offdevice.model.fit import load_model
from offdevice.model.score import score_bytes

DEFAULT_ARTIFACT = (Path(__file__).resolve().parents[1]
                    / "model" / "artifacts" / "mahalanobis.npz")
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"

# The fullest-ring base's distance as revealed by the one-shot holdout exam (the
# single alarm, banked as d=14.34). Reproducing it proves the loaded artifact and
# feature pipeline are the exact pair that graded that exam; any drift here means
# the eval would be scored by a different model than the one we shipped.
EXAM_ANCHOR = ("benign__tbA__nv15s-lab-steady1__run026__20260712T155435.bin",
               14.34, 0.01)

HEADLINE_TYPES = {"foreign_blob", "stride_break", "correlation_break",
                  "out_of_range_value", "nonmonotonic_ts"}
FLOOR_TYPES = {"journal_tamper"}
# Stated expectations, not failures -- aliases cover plausible collaborator naming.
DESIGNED_MISS_TYPES = {"region_erase", "whole_region_erase", "in_range_value",
                       "in_range_tweak", "settings_mimic", "perfect_settings_mimic",
                       "defaults_downgrade"}

PRETTY = {"stride_break": "stride break", "correlation_break": "correlation break",
          "out_of_range_value": "out-of-range value",
          "nonmonotonic_ts": "non-monotonic timestamp",
          "journal_tamper": "journal tamper"}


def bucket_of(typ: str) -> str:
    """Reporting bucket for one anomaly type; unknown types land in headline LOUDLY."""
    if typ in FLOOR_TYPES:
        return "floor"
    if typ in DESIGNED_MISS_TYPES:
        return "designed_miss"
    if typ not in HEADLINE_TYPES:
        print(f"[eval] NOTE: unknown type '{typ}' counted as headline -- "
              f"reclassify in score_anomalies.py if that is wrong")
    return "headline"


def read_manifest(path: Path) -> list[dict[str, object]]:
    """Manifest entries (the leading provenance line carries no 'file' key)."""
    entries = [json.loads(line) for line in path.read_text().splitlines() if line]
    return [e for e in entries if "file" in e]


def roc_curve(benign: NDArray[np.float64], anom: NDArray[np.float64],
              ) -> list[tuple[float, float]]:
    """(false-alarm rate, detection rate) as the cutoff slides; verdict is d > cutoff."""
    pts = [(0.0, 0.0)]
    for c in np.unique(np.concatenate([benign, anom]))[::-1]:
        pts.append((float(np.mean(benign > c)), float(np.mean(anom > c))))
    pts.append((1.0, 1.0))
    return pts


def auc_of(benign: NDArray[np.float64], anom: NDArray[np.float64]) -> float:
    """P(random anomaly outscores random benign); rank form is exact under ties."""
    from scipy.stats import rankdata
    ranks = rankdata(np.concatenate([benign, anom]))
    u = ranks[len(benign):].sum() - len(anom) * (len(anom) + 1) / 2
    return float(u / (len(anom) * len(benign)))


def type_key(entry: dict[str, object]) -> str:
    """Per-type reporting key; the blob sweep splits foreign_blob by exact size."""
    if entry["type"] == "foreign_blob":
        return f"foreign_blob_{entry['nv_changed_bytes']}B"
    return str(entry["type"])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Score an intake-verified anomaly delivery against the shipped model.")
    ap.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    ap.add_argument("--anomalies-dir", type=Path, default=DEFAULT_ANOMALIES_DIR)
    ap.add_argument("--captures-dir", type=Path, default=DEFAULT_CAPTURES_DIR)
    ap.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    args = ap.parse_args()

    model = load_model(args.artifact)
    if model.threshold is None:
        print("[eval] artifact has no threshold -- the eval is against the SHIPPED "
              "verdict line; fit one first")
        return 1
    thr = model.threshold

    loo_pairs = model.meta.get("loo_distances")
    if not loo_pairs:
        print("[eval] artifact carries no leave-one-out distances -- the benign side "
              "of the curve is missing; re-fit with a current fit.py")
        return 1
    benign = np.array([d for _, d in loo_pairs], dtype=np.float64)
    if len(benign) != model.meta.get("n_train"):
        print(f"[eval] {len(benign)} leave-one-out distances vs n_train="
              f"{model.meta.get('n_train')} -- artifact inconsistent")
        return 1

    manifest = args.anomalies_dir / MANIFEST_NAME
    if not manifest.exists():
        print(f"[eval] {manifest} not found -- run: python -m offdevice.eval.intake")
        return 1
    entries = read_manifest(manifest)
    if not entries:
        print("[eval] manifest names no files -- nothing to score")
        return 1

    # Anchor first: if the shipped pair can't reproduce the banked exam number,
    # every score below would be from some other model. Refuse to continue.
    anchor_name, anchor_d, tol = EXAM_ANCHOR
    got = score_bytes(model, (args.captures_dir / anchor_name).read_bytes())
    if abs(got - anchor_d) > tol:
        print(f"[eval] ANCHOR FAIL: {anchor_name} scored {got:.3f}, banked exam "
              f"value is {anchor_d} -- artifact/pipeline mismatch, eval aborted")
        return 1
    print(f"[eval] anchor OK: {anchor_name.split('__')[2]}/{anchor_name.split('__')[3]} "
          f"reproduces its banked exam distance ({got:.3f} ~ {anchor_d})")

    # Base scores, one per unique base -- the before-column of every result.
    base_d: dict[str, float] = {}
    for name in sorted({str(e["base"]) for e in entries}):
        raw = (args.captures_dir / name).read_bytes()
        base_d[name] = score_bytes(model, raw)

    buckets = {str(e["file"]): bucket_of(str(e["type"])) for e in entries}
    results: list[dict[str, object]] = []
    for e in sorted(entries, key=lambda e: (buckets[str(e["file"])], str(e["file"]))):
        path = args.anomalies_dir / str(e["file"])
        if not path.exists():
            print(f"[eval] {path.name} named in the manifest but missing on disk -- "
                  f"re-run intake first")
            return 1
        raw = path.read_bytes()
        if hashlib.md5(raw).hexdigest() != e["md5"]:
            print(f"[eval] {path.name} changed since intake -- re-run intake first")
            return 1
        d = score_bytes(model, raw)
        b = base_d[str(e["base"])]
        results.append({
            "file": e["file"], "type": e["type"], "type_key": type_key(e),
            "tier": e["tier"], "bucket": buckets[str(e["file"])],
            "base": e["base"], "nv_changed_bytes": e["nv_changed_bytes"],
            "base_d": b, "d": d, "delta": d - b,
            "flagged": bool(d > thr), "base_flagged": bool(b > thr),
        })

    # ---- console report ----------------------------------------------------
    print(f"\n[eval] threshold {thr:.3f} (the shipped verdict line); "
          f"benign side = {len(benign)} leave-one-out distances")
    for bucket, title in (("headline", "HEADLINE ANOMALIES"),
                          ("floor", "FLOOR MEASUREMENT (journal tampers -- not a headline type)"),
                          ("designed_miss", "DESIGNED MISSES (expected NOT to be caught)")):
        rows = [r for r in results if r["bucket"] == bucket]
        if not rows:
            continue
        print(f"\n== {title} ==")
        print(f"  {'file':<52} {'changed':>7}  {'base d':>7}  {'d':>8}  verdict")
        for r in rows:
            note = " (base already alarms untampered)" if r["base_flagged"] else ""
            print(f"  {str(r['file']):<52} {r['nv_changed_bytes']:>6}B  "
                  f"{r['base_d']:>7.3f}  {r['d']:>8.3f}  "
                  f"{'CAUGHT' if r['flagged'] else 'missed'}{note}")

    per_type: dict[str, dict[str, object]] = {}
    for r in results:
        t = per_type.setdefault(str(r["type_key"]), {
            "bucket": r["bucket"], "n": 0, "caught": 0, "distances": []})
        t["n"] = int(t["n"]) + 1
        t["caught"] = int(t["caught"]) + int(bool(r["flagged"]))
        t["distances"].append(float(r["d"]))  # type: ignore[union-attr]
    print("\n== PER-TYPE CATCH RATE AT THE SHIPPED THRESHOLD ==")
    for key, t in sorted(per_type.items(), key=lambda kv: (kv[1]["bucket"], kv[0])):
        ds = np.array(t["distances"])
        tag = "" if t["bucket"] == "headline" else f"  [{t['bucket']}]"
        print(f"  {key:<28} {t['caught']}/{t['n']} caught   "
              f"d min/med/max = {ds.min():.2f}/{float(np.median(ds)):.2f}/{ds.max():.2f}{tag}")

    head = [r for r in results if r["bucket"] == "headline"]
    if head:
        head_d = np.array([r["d"] for r in head], dtype=np.float64)
        caught = int(sum(bool(r["flagged"]) for r in head))
        pts = roc_curve(benign, head_d)
        auc = auc_of(benign, head_d)
        op = (float(np.mean(benign > thr)), caught / len(head))
        print("\n== HEADLINE SUMMARY ==")
        print(f"  caught {caught} of {len(head)} at threshold {thr:.3f} "
              f"({op[1]:.0%} detection, {op[0]:.1%} benign false alarms)")
        print(f"  AUC {auc:.3f} (0.5 = coin flip, 1.0 = perfect separation)")
    else:
        caught, pts, auc, op = 0, [], None, None
        print("\n[eval] no headline anomalies in this delivery -- ROC/AUC skipped")

    # ---- files -------------------------------------------------------------
    args.results_dir.mkdir(parents=True, exist_ok=True)
    out = {
        "artifact": str(args.artifact), "threshold": thr,
        "n_train": model.meta.get("n_train"),
        "benign_side": f"{len(benign)} leave-one-out distances from the artifact",
        "anchor_check": {"file": anchor_name, "banked": anchor_d,
                         "scored": round(got, 3), "pass": True},
        "base_scores": {k: round(v, 3) for k, v in base_d.items()},
        "per_file": results,
        "per_type": {k: {"bucket": t["bucket"], "n": t["n"], "caught": t["caught"]}
                     for k, t in per_type.items()},
        "headline": ({"n": len(head), "caught": caught, "auc": auc,
                      "operating_point": {"false_alarm_rate": op[0],
                                          "detection_rate": op[1]}}
                     if head else None),
        "roc_points": pts or None,
    }
    results_json = args.results_dir / "eval_results.json"
    results_json.write_text(json.dumps(out, indent=2) + "\n", newline="\n")
    print(f"\n[eval] results -> {results_json}")

    rows = report_plots.build_rows(benign, results, PRETTY)
    figures = [report_plots.score_line(rows, thr, args.results_dir / "score_line.png")]
    blob_pts = [(int(r["nv_changed_bytes"]), float(r["d"]), bool(r["flagged"]))
                for r in results if r["type"] == "foreign_blob"]
    if blob_pts:
        figures.append(report_plots.blob_sweep(
            blob_pts, thr, args.results_dir / "blob_sweep.png"))
    if head:
        figures.append(report_plots.roc_appendix(
            pts, auc, op, thr, args.results_dir / "roc_appendix.png"))
    for p in figures:
        print(f"[eval] figure  -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
