"""
Holdout split: lock ~20% of the benign captures away BEFORE any model fitting.

The chosen filenames go to a committed .txt so "the exam set was picked before
studying" is a verifiable property of the repo, not a promise: fit.py excludes these
files from training and from the threshold, and score.py grades them exactly once,
after the threshold is chosen, as the honest false-positive check. The pick is
stratified by ring fill state so every benign regime appears on the exam, and the
file records WHICH variants the split saw -- fit.py refuses to train on variants
beyond that set, because their captures would carry zero exam coverage. An existing
list is never overwritten without --force -- a re-split invalidates any model already
fitted against the old one.

Choose the split (writes offdevice/data/holdout.txt, which gets committed):
    python -m offdevice.model.split nv45s-lab-fill1 nv45s-lab-w1
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import numpy as np

from offdevice.model.dataset import (
    DEFAULT_MANIFEST,
    DEFAULT_QUARANTINE,
    FILL_STATES,
    Sample,
    load_samples,
    read_name_list,
    read_quarantine,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOLDOUT = REPO_ROOT / "offdevice" / "data" / "holdout.txt"

DEFAULT_FRACTION = 0.20
DEFAULT_SEED = 2026

# The machine-readable header line recording the variants the split saw.
_VARIANTS_PREFIX = "# variants:"


def read_holdout(path: str | Path) -> frozenset[str]:
    """The held-out bare filenames from a holdout .txt ('#' comments ignored)."""
    return read_name_list(path)


def read_holdout_variants(path: str | Path) -> frozenset[str] | None:
    """The variants the split saw, from the '# variants:' header (None if absent).

    fit.py refuses variants outside this set: the split never stratified them, so
    every one of their captures would train with zero holdout-exam coverage.
    """
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        if raw.startswith(_VARIANTS_PREFIX):
            names = raw[len(_VARIANTS_PREFIX):].split(",")
            return frozenset(v.strip() for v in names if v.strip())
    return None


def choose_holdout(
    samples: list[Sample], fraction: float, seed: int
) -> tuple[list[Sample], list[str]]:
    """Stratified pick: per fill state, max(1, round(fraction*n)) capped at n-1.

    A single-capture stratum stays entirely in training -- holding out its only
    example would leave that benign state unlearned, a worse trade than losing its
    exam coverage. Returns (chosen samples, human notes for the file header).
    """
    if not (0.0 < fraction < 1.0):
        raise ValueError(f"fraction must be in (0, 1), got {fraction}")
    rng = np.random.default_rng(seed)
    by_state: dict[str, list[Sample]] = {}
    for s in sorted(samples, key=lambda s: s.record.file):
        by_state.setdefault(s.fill_state, []).append(s)

    chosen: list[Sample] = []
    notes: list[str] = []
    for state in FILL_STATES:
        group = by_state.get(state, [])
        n = len(group)
        if n == 0:
            continue
        if n == 1:
            notes.append(f"{state}: only 1 capture -- kept in training")
            continue
        k = min(n - 1, max(1, round(fraction * n)))
        idx = sorted(rng.choice(n, size=k, replace=False).tolist())
        chosen.extend(group[i] for i in idx)
        notes.append(f"{state}: held out {k} of {n}")
    return chosen, notes


def write_holdout(
    path: Path, chosen: list[Sample], variants: tuple[str, ...],
    fraction: float, seed: int, n_total: int, notes: list[str],
) -> None:
    """Write the holdout .txt: a self-documenting header, then one filename per line."""
    lines = [
        "# Benign holdout list -- the locked-away exam set.",
        "# These captures never touch a model fit or a threshold; they are scored",
        "# exactly once (offdevice/model/score.py), after the threshold is chosen,",
        "# as the honest false-positive check. Everything else in the manifest with",
        "# the variants below is training data.",
        # Machine-read by read_holdout_variants -- keep the "# variants:" prefix.
        f"{_VARIANTS_PREFIX} {','.join(variants)}",
        f"# created={datetime.now().isoformat(timespec='seconds')} seed={seed} "
        f"fraction={fraction}",
        f"# {len(chosen)} of {n_total} captures held out -- " + "; ".join(notes),
    ]
    lines += [f"{s.record.file}  # {s.fill_state}" for s in chosen]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Choose the stratified holdout set and write it to a committed .txt.")
    ap.add_argument("variants", nargs="+", help="campaign tags, e.g. nv45s-lab-fill1 nv45s-lab-w1")
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--out", type=Path, default=DEFAULT_HOLDOUT,
                    help=f"holdout .txt to write (default {DEFAULT_HOLDOUT})")
    ap.add_argument("--fraction", type=float, default=DEFAULT_FRACTION,
                    help="held-out share per stratum (default 0.20)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                    help="RNG seed -- recorded in the file so the pick is reproducible")
    ap.add_argument("--allow-out-of-range", action="store_true",
                    help="split despite captures whose values leave their legal ranges")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing list (invalidates any already-fitted model)")
    args = ap.parse_args()

    if args.out.exists() and not args.force:
        print(f"[split] {args.out} already exists -- the split is chosen ONCE, before any "
              f"fitting. Re-splitting invalidates fitted models; pass --force only if that "
              f"is deliberate.")
        return 1

    quarantine = read_quarantine()
    if quarantine:
        print(f"[split] {len(quarantine)} quarantined name(s) excluded "
              f"({DEFAULT_QUARANTINE.name})")
    samples = load_samples(args.manifest, tuple(args.variants), quarantine=quarantine)
    if len(samples) < 2:
        print(f"[split] need at least 2 matching captures, found {len(samples)}")
        return 1

    # Symmetric with fit.py's training refusal: an out-of-range capture placed in
    # the HOLDOUT would score as a large distance later and inflate the reported
    # false-positive rate of a threshold that never saw its like.
    bad_range = [s.record.file for s in samples if not s.range_ok]
    if bad_range and not args.allow_out_of_range:
        print(f"[split] {len(bad_range)} capture(s) hold out-of-range values -- holding "
              f"one out would pollute the false-positive exam. Inspect them "
              f"(offdevice.model.dataset) or pass --allow-out-of-range: "
              f"{bad_range[:3]}{'...' if len(bad_range) > 3 else ''}")
        return 1

    chosen, notes = choose_holdout(samples, args.fraction, args.seed)
    if not chosen:
        print("[split] nothing can be held out safely (every fill state has a single "
              "capture) -- collect more data first; an empty exam set is not a split")
        return 1
    write_holdout(args.out, chosen, tuple(args.variants), args.fraction, args.seed,
                  len(samples), notes)
    print(f"[split] {len(chosen)} of {len(samples)} captures -> {args.out}")
    for note in notes:
        print(f"[split]   {note}")
    print("[split] commit this file BEFORE fitting -- it is the proof the exam was "
          "locked away first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
