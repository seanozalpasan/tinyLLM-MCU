"""
Holdout split: lock ~20% of the benign captures away BEFORE any model fitting.

The chosen filenames go to a committed .txt so "the exam set was picked before
studying" is a verifiable property of the repo, not a promise: fit.py excludes these
files from training and from the threshold, and score.py grades them exactly once,
after the threshold is chosen, as the honest false-positive check. The pick is
stratified by ring fill state x settings state (pages carrying journal change
entries are their own benign regime) so every regime appears on the exam, and the
file records WHICH variants the split saw -- fit.py refuses to train on variants
beyond that set, because their captures would carry zero exam coverage. An existing
list is never overwritten without --force -- a re-split invalidates any model already
fitted against the old one.

--pin forces a name-list's captures into the holdout -- any capture that must sit
in the exam rather than in training qualifies, with the pin file recording each
name's reason. The founding case is the collaborator's anomaly bases: every
returned anomaly is a tampered copy of one, so the model must never have trained
on the file under the tampering, or the detection numbers carry a memorized-base
confound. 'md5=' comments in the pin file are verified
against the manifest -- the file being tampered must be byte-for-byte the file
locked out of training.

Choose the split (writes offdevice/data/holdout.txt, which gets committed):
    python -m offdevice.model.split nv15s-lab-fill1 nv15s-lab-steady1 --pin offdevice/data/collab_bases.txt
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

import numpy as np

from offdevice.model.dataset import (
    DEFAULT_MANIFEST,
    DEFAULT_QUARANTINE,
    FILL_STATES,
    SETTINGS_STATES,
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


_MD5_RE = re.compile(r"\bmd5=([0-9a-fA-F]{32})\b")


def read_pin_md5s(path: str | Path) -> dict[str, str]:
    """name -> recorded md5, for pin-file lines whose comment carries 'md5=...'.

    collab_bases.txt records each base's md5 as it was MAILED; the split verifies
    the banked capture still matches before pinning, or the file the collaborator
    is tampering is not the file being locked out of training. Lines without an
    md5 comment simply aren't checked.
    """
    out: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        name, _, comment = raw.partition("#")
        name = name.strip()
        m = _MD5_RE.search(comment)
        if name and m:
            out[name] = m.group(1).lower()
    return out


def choose_holdout(
    samples: list[Sample], fraction: float, seed: int,
    pinned: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[Sample], list[str]]:
    """Stratified pick: per (fill state, settings state) stratum,
    max(1, round(fraction*n)) capped at n-1; `pinned` names are forced in.

    A single-capture stratum stays entirely in training -- holding out its only
    example would leave that benign state unlearned, a worse trade than losing its
    exam coverage. Pinned captures (the collaborator's anomaly bases: every
    returned anomaly is a tampered copy of one, so the model must never train on
    them) take their stratum's exam seats first and widen the quota if they
    outnumber it -- but never past the n-1 cap, and never out of a singleton
    stratum. Returns (chosen samples, human notes for the file header).
    """
    if not (0.0 < fraction < 1.0):
        raise ValueError(f"fraction must be in (0, 1), got {fraction}")
    known = {Path(s.record.file).name for s in samples}
    missing = sorted(set(pinned) - known)
    if missing:
        raise ValueError(f"pinned name(s) not among the loaded captures: {missing}")
    rng = np.random.default_rng(seed)
    by_stratum: dict[str, list[Sample]] = {}
    for s in sorted(samples, key=lambda s: s.record.file):
        by_stratum.setdefault(f"{s.fill_state}/{s.settings_state}", []).append(s)

    chosen: list[Sample] = []
    notes: list[str] = []
    for fill in FILL_STATES:
        for setting in SETTINGS_STATES:
            stratum = f"{fill}/{setting}"
            group = by_stratum.get(stratum, [])
            n = len(group)
            if n == 0:
                continue
            pin_idx = [i for i, s in enumerate(group)
                       if Path(s.record.file).name in pinned]
            if n == 1:
                if pin_idx:
                    raise ValueError(f"{stratum}: its only capture is pinned -- "
                                     f"holding it out would leave that benign state "
                                     f"unlearned")
                notes.append(f"{stratum}: only 1 capture -- kept in training")
                continue
            if len(pin_idx) > n - 1:
                raise ValueError(f"{stratum}: {len(pin_idx)} pinned of {n} -- training "
                                 f"must keep at least one capture of every benign state")
            k = max(min(n - 1, max(1, round(fraction * n))), len(pin_idx))
            pool = [i for i in range(n) if i not in pin_idx]
            extra = rng.choice(len(pool), size=k - len(pin_idx), replace=False)
            idx = sorted(pin_idx + [pool[i] for i in extra.tolist()])
            chosen.extend(group[i] for i in idx)
            note = f"{stratum}: held out {k} of {n}"
            if pin_idx:
                note += f" ({len(pin_idx)} pinned)"
            notes.append(note)
    return chosen, notes


def write_holdout(
    path: Path, chosen: list[Sample], variants: tuple[str, ...],
    fraction: float, seed: int, n_total: int, notes: list[str],
    pinned: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Write the holdout .txt: a self-documenting header, then one filename per line."""
    lines = [
        "# Benign holdout list -- the locked-away exam set.",
        "# These captures never touch a model fit or a threshold; they are scored",
        "# exactly once (offdevice/model/score.py), after the threshold is chosen,",
        "# as the honest false-positive check. Everything else in the manifest with",
        "# the variants below is training data.",
    ]
    if pinned:
        lines += [
            "# 'pinned' = a capture the split's --pin list forced into the exam; the",
            "# pin file records each name's reason (e.g. a collaborator anomaly base",
            "# the model must never train on, or a deliberately capped subsample).",
        ]
    lines += [
        # Machine-read by read_holdout_variants -- keep the "# variants:" prefix.
        f"{_VARIANTS_PREFIX} {','.join(variants)}",
        f"# created={datetime.now().isoformat(timespec='seconds')} seed={seed} "
        f"fraction={fraction}",
        f"# {len(chosen)} of {n_total} captures held out -- " + "; ".join(notes),
    ]
    for s in chosen:
        tag = "  pinned" if Path(s.record.file).name in pinned else ""
        lines.append(f"{s.record.file}  # {s.fill_state}/{s.settings_state}{tag}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Choose the stratified holdout set and write it to a committed .txt.")
    ap.add_argument("variants", nargs="+", help="campaign tags, e.g. nv15s-lab-fill1 nv15s-lab-steady1")
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--out", type=Path, default=DEFAULT_HOLDOUT,
                    help=f"holdout .txt to write (default {DEFAULT_HOLDOUT})")
    ap.add_argument("--fraction", type=float, default=DEFAULT_FRACTION,
                    help="held-out share per stratum (default 0.20)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                    help="RNG seed -- recorded in the file so the pick is reproducible")
    ap.add_argument("--pin", type=Path, default=None,
                    help="name-list .txt (collab_bases.txt format) whose captures are "
                         "forced into the holdout; 'md5=' comments are verified "
                         "against the manifest")
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

    pinned: frozenset[str] = frozenset()
    if args.pin is not None:
        pinned = read_name_list(args.pin)
        if not pinned:
            print(f"[split] {args.pin} names no captures -- nothing to pin")
            return 1
        by_name = {Path(s.record.file).name: s for s in samples}
        absent = sorted(pinned - by_name.keys())
        if absent:
            print(f"[split] pinned name(s) not among the loaded captures (wrong tag, "
                  f"quarantined, or not banked?): {absent}")
            return 1
        for name, mailed in read_pin_md5s(args.pin).items():
            banked = by_name[name].record.md5.lower()
            if banked != mailed:
                print(f"[split] {name}: banked md5 {banked} != mailed md5 {mailed} -- "
                      f"the file the collaborator is tampering is not the banked "
                      f"capture; resolve before locking the exam")
                return 1
        print(f"[split] pinning {len(pinned)} capture(s) from {args.pin} into the "
              f"holdout (md5s verified against the manifest)")

    chosen, notes = choose_holdout(samples, args.fraction, args.seed, pinned=pinned)
    if not chosen:
        print("[split] nothing can be held out safely (every fill state has a single "
              "capture) -- collect more data first; an empty exam set is not a split")
        return 1
    write_holdout(args.out, chosen, tuple(args.variants), args.fraction, args.seed,
                  len(samples), notes, pinned=pinned)
    print(f"[split] {len(chosen)} of {len(samples)} captures -> {args.out}")
    for note in notes:
        print(f"[split]   {note}")
    print("[split] commit this file BEFORE fitting -- it is the proof the exam was "
          "locked away first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
