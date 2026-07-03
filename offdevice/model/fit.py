"""
One-class Mahalanobis fit over benign NV feature vectors -- the model half of the
model+threshold contract the on-chip scorer must reproduce.

The model is a benign mean vector plus the PRECISION (inverse covariance) matrix of a
Ledoit-Wolf-shrunk covariance; the anomaly score of a flat feature vector x is

    d(x) = sqrt( (x - mean)^T @ precision @ (x - mean) )

exactly this arithmetic. (On-chip, compare d^2 against threshold^2 to skip the sqrt --
monotonic, so the verdicts are identical.) Shrinkage matters because we fit tens of
samples in 120 dimensions: a raw sample covariance there is singular; Ledoit-Wolf
blends it toward a scaled identity just enough to be well-conditioned, with the blend
weight estimated from the data itself. The blend runs on per-dimension standardized
features and the scaling is folded back into the shipped precision (fit_mean_precision)
-- so the formula above IS the whole on-chip contract.

The reported distance distribution -- and any threshold drawn from it -- comes from
LEAVE-ONE-OUT distances: each sample is scored by a model fitted WITHOUT it. In-sample
distances are forbidden here: with n << dims a model scores its own training data
flatteringly close, and a threshold drawn from that would false-positive in the field.
(The LOO models see n-1 samples while the shipped model sees n, so the threshold is
marginally conservative for it -- the safe direction, and the holdout check catches
any surprise.)

Fit (excludes the committed holdout list; report first, threshold once agreed):
    python -m offdevice.model.fit nv45s-lab-fill1 nv45s-lab-w1
    python -m offdevice.model.fit nv45s-lab-fill1 nv45s-lab-w1 --fp-target 0.05
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version as pkg_version
from pathlib import Path

import numpy as np
import numpy.typing as npt
from sklearn.covariance import LedoitWolf

from offdevice.data.manifest import read_manifest
from offdevice.features import params
from offdevice.model.dataset import (
    DEFAULT_MANIFEST,
    FILL_STATES,
    N_DIMS,
    Sample,
    design_matrix,
    load_samples,
)
from offdevice.model.split import DEFAULT_HOLDOUT, read_holdout
from offdevice.nv import spec

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT = REPO_ROOT / "offdevice" / "model" / "artifacts" / "mahalanobis"

# Candidate false-positive targets shown in every report, so the threshold decision
# is made looking at concrete numbers rather than an abstract quantile.
CANDIDATE_FP_TARGETS = (0.10, 0.05, 0.02, 0.01)

Vector = npt.NDArray[np.float64]
Matrix = npt.NDArray[np.float64]


@dataclass(frozen=True)
class MahalanobisModel:
    """The fitted one-class model: everything the on-chip scorer needs, exactly."""

    mean: Vector          # (N_DIMS,)
    precision: Matrix     # (N_DIMS, N_DIMS), symmetric positive-definite
    threshold: float | None   # anomaly if d(x) > threshold; None until agreed
    meta: dict[str, object]   # provenance: samples, shrinkage, pins, versions


def fit_mean_precision(x_train: Matrix) -> tuple[Vector, Matrix, float]:
    """Ledoit-Wolf fit on per-dimension standardized features: (mean, precision, shrinkage).

    GOTCHA: the 120 dims live on wildly different scales (MFCC ~1e2, mel power
    ~1e-6), and Ledoit-Wolf shrinks toward ONE common variance for every dim. On the
    raw scales that would assign the tiny-scale dims a huge presumed wobble --
    silencing mel+chroma (80 of 120 dims) in the distance. So the blend happens on
    standardized features (each dim in units of its own training spread), and the
    scaling folds back into the returned precision (P = D^-1 P_z D^-1, D = diag(std)):
    the downstream score arithmetic and export format are unchanged.
    """
    if x_train.ndim != 2 or x_train.shape[0] < 3:
        raise ValueError(f"need a (n>=3, dims) matrix, got {x_train.shape}")
    std = x_train.std(axis=0)
    if not np.any(std > 0):
        raise ValueError("every feature dimension is constant -- nothing to fit")
    mean = x_train.mean(axis=0).astype(np.float64)
    # A constant dim has no spread to standardize by; scale 1 keeps it inert during
    # the fit (all-zero column) while test-time movement in it still scores.
    scale = np.where(std > 0, std, 1.0)
    z = (x_train - mean) / scale
    lw = LedoitWolf(assume_centered=True).fit(z)   # z is exactly mean-0 by construction
    precision = np.asarray(lw.precision_, dtype=np.float64) / np.outer(scale, scale)
    precision = (precision + precision.T) / 2.0   # exact symmetry for the export
    return mean, precision, float(lw.shrinkage_)


def distances(mean: Vector, precision: Matrix, x: Matrix) -> Vector:
    """Mahalanobis distance of each row of x -- THE scoring arithmetic (see header)."""
    diff = np.atleast_2d(x) - mean
    d2 = np.einsum("ij,jk,ik->i", diff, precision, diff)
    return np.sqrt(np.clip(d2, 0.0, None))   # clip: tiny negatives from float round-off


def loo_distances(x_train: Matrix) -> Vector:
    """Leave-one-out distances: row i scored by a model fitted without row i.

    n refits of a tiny model -- seconds. These are the only honest benign distances
    a small training set can produce, and the only ones a threshold may come from.
    """
    n = x_train.shape[0]
    if n < 4:
        raise ValueError(f"leave-one-out needs >= 4 samples, got {n}")
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        rest = np.delete(x_train, i, axis=0)
        mean, precision, _ = fit_mean_precision(rest)
        out[i] = distances(mean, precision, x_train[i])[0]
    return out


def threshold_from_loo(loo: Vector, fp_target: float) -> float:
    """The alarm line: the (1 - fp_target) quantile of the leave-one-out distances.

    method="higher" picks an actual observed distance at or above the quantile --
    conservative (errs toward fewer false alarms) and exactly reproducible on-chip.
    """
    if not (0.0 < fp_target < 1.0):
        raise ValueError(f"fp_target must be in (0, 1), got {fp_target}")
    return float(np.quantile(loo, 1.0 - fp_target, method="higher"))


# ---- artifact I/O ------------------------------------------------------------------


def save_model(model: MahalanobisModel, stem: str | Path) -> tuple[Path, Path]:
    """Write <stem>.npz (arrays, the machine artifact) + <stem>.json (human sidecar)."""
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    npz_path = stem.with_suffix(".npz")
    json_path = stem.with_suffix(".json")
    threshold = np.float64(np.nan if model.threshold is None else model.threshold)
    np.savez(npz_path, mean=model.mean, precision=model.precision, threshold=threshold,
             meta_json=np.array(json.dumps(model.meta)))
    json_path.write_text(
        json.dumps({"threshold": model.threshold, **model.meta}, indent=2) + "\n",
        encoding="utf-8")
    return npz_path, json_path


def load_model(path: str | Path) -> MahalanobisModel:
    """Load a saved artifact (either the .npz or its bare stem)."""
    path = Path(path)
    if path.suffix != ".npz":
        path = path.with_suffix(".npz")
    with np.load(path, allow_pickle=False) as z:
        threshold = float(z["threshold"])
        return MahalanobisModel(
            mean=z["mean"],
            precision=z["precision"],
            threshold=None if np.isnan(threshold) else threshold,
            meta=json.loads(str(z["meta_json"])),
        )


# ---- CLI: fit + report -------------------------------------------------------------


def _state_counts(samples: list[Sample]) -> str:
    counts: dict[str, int] = {}
    for s in samples:
        counts[s.fill_state] = counts.get(s.fill_state, 0) + 1
    return "  ".join(f"{st}={counts[st]}" for st in FILL_STATES if st in counts)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fit the benign Mahalanobis model; report leave-one-out distances.")
    ap.add_argument("variants", nargs="+", help="campaign tags to train on")
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--holdout", type=Path, default=DEFAULT_HOLDOUT,
                    help=f"holdout .txt to EXCLUDE (default {DEFAULT_HOLDOUT})")
    ap.add_argument("--no-holdout", action="store_true",
                    help="train on everything -- plumbing checks only, never a real model")
    ap.add_argument("--fp-target", type=float, default=None,
                    help="false-positive target that sets the threshold (e.g. 0.05); "
                         "omit to report the distance distribution without choosing")
    ap.add_argument("--allow-out-of-range", action="store_true",
                    help="fit despite captures whose values leave their legal ranges")
    ap.add_argument("--out", type=Path, default=DEFAULT_ARTIFACT,
                    help=f"artifact stem to write (default {DEFAULT_ARTIFACT})")
    args = ap.parse_args()

    if args.no_holdout:
        exclude: frozenset[str] = frozenset()
        holdout_name = None
        print("[fit] --no-holdout: training on ALL matching captures. Plumbing only -- "
              "a real model must exclude the committed holdout list.")
    else:
        if not args.holdout.exists():
            print(f"[fit] holdout list {args.holdout} not found -- run "
                  f"offdevice.model.split first (or pass --no-holdout for plumbing).")
            return 1
        exclude = read_holdout(args.holdout)
        holdout_name = str(args.holdout)
        # A holdout list that excludes nothing REAL would silently train on the exam
        # set -- the one failure mode this workflow exists to prevent. Names must
        # match current benign captures of exactly these variants.
        if not exclude:
            print(f"[fit] {args.holdout} names no files -- stale or emptied; re-run split.")
            return 1
        in_scope = {r.file for r in read_manifest(args.manifest)
                    if r.label == "benign"
                    and r.conditions.get("variant") in set(args.variants)}
        stale = sorted(exclude - in_scope)
        if stale:
            print(f"[fit] {len(stale)} holdout entr{'y' if len(stale) == 1 else 'ies'} "
                  f"match no benign capture of variants {args.variants} -- the list is "
                  f"stale or from a different campaign (e.g. a smoke split). Re-run "
                  f"split against THIS data first: {stale[:4]}")
            return 1

    samples = load_samples(args.manifest, tuple(args.variants), exclude=exclude)
    if len(samples) < 4:
        print(f"[fit] {len(samples)} training capture(s) match variants {args.variants} "
              f"-- leave-one-out needs at least 4")
        return 1
    bad_range = [s.record.file for s in samples if not s.range_ok]
    if bad_range and not args.allow_out_of_range:
        print(f"[fit] {len(bad_range)} capture(s) hold out-of-range values (generator "
              f"bug era?) -- inspect them (offdevice.model.dataset) or pass "
              f"--allow-out-of-range: {bad_range[:3]}{'...' if len(bad_range) > 3 else ''}")
        return 1

    x_train = design_matrix(samples)
    n = x_train.shape[0]
    print(f"[fit] {n} training captures ({_state_counts(samples)}); "
          f"{len(exclude)} names excluded as holdout")

    flat = np.flatnonzero(x_train.std(axis=0) == 0)
    if flat.size:
        print(f"[fit] WARNING: {flat.size} of {N_DIMS} feature dims constant across "
              f"training (indices {flat[:8].tolist()}...) -- degenerate features or "
              f"too-similar captures")

    loo = loo_distances(x_train)
    q = {p: float(np.quantile(loo, p)) for p in (0.50, 0.90, 0.95)}
    print(f"[fit] leave-one-out distances: n={n} min={loo.min():.3f} "
          f"p50={q[0.50]:.3f} p90={q[0.90]:.3f} p95={q[0.95]:.3f} max={loo.max():.3f}")
    for fp in CANDIDATE_FP_TARGETS:
        marker = " (below 1/n resolution -- effectively the max)" if fp < 1.0 / n else ""
        print(f"[fit]   fp-target {fp:>5.0%} -> threshold {threshold_from_loo(loo, fp):.3f}"
              f"{marker}")

    threshold = None
    if args.fp_target is not None:
        threshold = threshold_from_loo(loo, args.fp_target)
        if args.fp_target < 1.0 / n:
            print(f"[fit] NOTE: fp-target {args.fp_target:.0%} is finer than 1/{n} -- "
                  f"the threshold is just the max observed distance; more data sharpens it")
        print(f"[fit] threshold = {threshold:.3f} (fp-target {args.fp_target:.0%})")

    mean, precision, shrinkage = fit_mean_precision(x_train)
    print(f"[fit] final model on all {n} training rows (Ledoit-Wolf shrinkage "
          f"{shrinkage:.3f})")

    model = MahalanobisModel(mean=mean, precision=precision, threshold=threshold, meta={
        "created": datetime.now().isoformat(timespec="seconds"),
        "n_train": n,
        "variants": list(args.variants),
        "holdout_file": holdout_name,
        "fp_target": args.fp_target,
        "shrinkage": shrinkage,
        "loo_distances": [round(float(d), 6) for d in sorted(loo)],
        "score": "d(x) = sqrt((x-mean)^T precision (x-mean)); anomaly if d > threshold",
        "standardization": "fit ran on per-dim z-scores; folded back: P = D^-1 P_z D^-1",
        "vector_order": f"feature-major {list(params.FEATURE_ORDER)}, {N_DIMS} dims",
        "feature_pins": {"n_fft": params.N_FFT, "hop": params.HOP_LENGTH,
                         "sr": params.SR, "window_bytes": params.WINDOW_BYTES},
        "nv_spec_version": spec.SPEC_VERSION,
        "versions": {p: pkg_version(p)
                     for p in ("numpy", "scipy", "scikit-learn", "librosa")},
    })
    npz_path, json_path = save_model(model, args.out)
    print(f"[fit] wrote {npz_path} + {json_path}")
    if threshold is not None and holdout_name is not None:
        print(f"[fit] next: score the holdout ONCE -- python -m offdevice.model.score "
              f"{npz_path} --holdout {holdout_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
