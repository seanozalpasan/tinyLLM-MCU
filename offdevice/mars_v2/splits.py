"""Dataset listing + stratified splits for mars_v2.

trainable_files() honors holdout.txt and quarantine.txt (never a blind glob);
stratified_calib_split() splits per fill_state x settings_state stratum so rare
ring regimes (near-empty / pre-wrap) appear on both sides of every split.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from offdevice.nv import spec
from offdevice.nv.parse import (
    RegionView, journal_chain, parse_region, records_chronological, slice_nv,
)

from .paths import CAPTURES, HOLDOUT_TXT, QUARANTINE_TXT

CALIB_FRACTION = 1 / 3


def read_names(path: Path) -> frozenset[str]:
    """Bare names from a list file; '#' starts a comment; missing file = empty."""
    if not path.exists():
        return frozenset()
    out = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        n = raw.partition("#")[0].strip()
        if n:
            out.add(n)
    return frozenset(out)


def trainable_files() -> list[Path]:
    """Benign captures minus holdout minus quarantine."""
    holdout = read_names(HOLDOUT_TXT)
    quarantine = read_names(QUARANTINE_TXT)
    return [f for f in sorted(CAPTURES.glob("benign__*.bin"))
            if f.name not in holdout and f.name not in quarantine]


def fill_state(view: RegionView) -> str:
    """Ring fill-state stratification label -- the model never sees it.

    page_seq counts page-opens, so a seq above NUM_PAGES means some page was
    erased and reopened (the ring wrapped); the first wrap cycle still carries
    a virgin-fill header mix, hence the just-wrapped stratum before steady.
    """
    if view.current is None:
        return "empty"
    header = view.pages[view.current].header
    assert header is not None   # current is by construction a valid-header page
    max_seq = header["page_seq"]
    if max_seq <= spec.NUM_PAGES:
        n = len(records_chronological(view))
        return "near-empty" if n <= spec.RECORDS_PER_PAGE // 2 else "pre-wrap"
    if max_seq <= 2 * spec.NUM_PAGES:
        return "just-wrapped"
    return "steady"


def settings_state(view: RegionView) -> str:
    """'settings-changed' when any journal chain holds an entry beyond J0."""
    for page in view.pages:
        if page.header is not None and len(journal_chain(page)) > 1:
            return "settings-changed"
    return "settings-quiet"


def stratum_of(path: Path) -> str:
    view = parse_region(slice_nv(path.read_bytes()))
    return f"{fill_state(view)}/{settings_state(view)}"


def stratified_calib_split(files: list[Path], seed: int,
                           fraction: float = CALIB_FRACTION,
                           ) -> tuple[list[Path], list[Path]]:
    """(fit_files, calib_files): per-stratum pick.

    Per stratum: max(1, round(fraction * n)) capped at n - 1; a singleton
    stratum stays entirely in fit (its regime must be learned; it cannot also
    calibrate).
    """
    rng = np.random.default_rng(seed)
    by_stratum: dict[str, list[Path]] = {}
    for f in sorted(files, key=lambda p: p.name):
        by_stratum.setdefault(stratum_of(f), []).append(f)
    fit: list[Path] = []
    calib: list[Path] = []
    for stratum in sorted(by_stratum):
        group = by_stratum[stratum]
        n = len(group)
        if n == 1:
            fit.extend(group)
            continue
        k = min(n - 1, max(1, round(fraction * n)))
        idx = set(rng.choice(n, size=k, replace=False).tolist())
        for i, f in enumerate(group):
            (calib if i in idx else fit).append(f)
    return fit, calib
