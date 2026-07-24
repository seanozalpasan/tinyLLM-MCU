"""
Dataset listing + stratified splits for mars_v2.

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
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.partition("#")[0].strip()
        if name:
            names.add(name)
    return frozenset(names)


def trainable_files() -> list[Path]:
    """Benign captures minus holdout minus quarantine."""
    holdout_names = read_names(HOLDOUT_TXT)
    quarantine_names = read_names(QUARANTINE_TXT)
    return [file_path for file_path in sorted(CAPTURES.glob("benign__*.bin"))
            if file_path.name not in holdout_names
            and file_path.name not in quarantine_names]


def fill_state(view: RegionView) -> str:
    """How full the ring is -- used only to label captures for splitting.

    page_seq counts page-opens, so a seq above NUM_PAGES means some page was
    erased and reopened (the ring wrapped). The first wrap cycle still carries
    a mix of virgin-fill headers, so it gets its own just-wrapped label before
    steady state. The model itself never sees these labels.
    """
    if view.current is None:
        return "empty"
    header = view.pages[view.current].header
    assert header is not None   # current is by construction a valid-header page
    newest_page_seq = header["page_seq"]
    if newest_page_seq <= spec.NUM_PAGES:
        record_count = len(records_chronological(view))
        return ("near-empty" if record_count <= spec.RECORDS_PER_PAGE // 2
                else "pre-wrap")
    if newest_page_seq <= 2 * spec.NUM_PAGES:
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
    """(fit_files, calib_files): a per-stratum pick.

    Per stratum: max(1, round(fraction * n)) capped at n - 1. A stratum with a
    single capture stays entirely in fit -- its regime has to be learned, and
    one file cannot be on both sides of a split.
    """
    random_gen = np.random.default_rng(seed)
    by_stratum: dict[str, list[Path]] = {}
    for file_path in sorted(files, key=lambda p: p.name):
        by_stratum.setdefault(stratum_of(file_path), []).append(file_path)
    fit_files: list[Path] = []
    calib_files: list[Path] = []
    for stratum in sorted(by_stratum):
        group = by_stratum[stratum]
        group_size = len(group)
        if group_size == 1:
            fit_files.extend(group)
            continue
        calib_count = min(group_size - 1, max(1, round(fraction * group_size)))
        chosen = set(random_gen.choice(group_size, size=calib_count,
                                       replace=False).tolist())
        for position, file_path in enumerate(group):
            (calib_files if position in chosen else fit_files).append(file_path)
    return fit_files, calib_files
