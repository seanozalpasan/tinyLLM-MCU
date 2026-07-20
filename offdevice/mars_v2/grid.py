"""V2 record grid: nv_grid with the finding-2/4/9 encoding fixes.

Same (RECORDS_TOTAL, 5) shape and channel normalization as V1's nv_grid, so the
frozen AE architecture is unchanged. Three encoding changes:

  ts_delta (finding 2 + 9): V1 clipped every backwards step to -2.0, making the
      benign boot-reset seam byte-identical to a mid-stream regression, and
      clipped forward gaps at 4.0, saturating a 10000 s gap to the same value as
      a benign missed-sample gap. V2 encodes:
        - benign seam (non-increasing ts, landing <= LANDING_LIMIT_S)  -> -1.0
        - mid-stream regression (landing beyond the seam window)       -> -2.0
        - forward gaps: dt/PERIOD up to 4, then log-compressed
          4 + log2(dt/4), capped at 8 -- large gaps stay visible and ordered
          instead of aliasing onto the benign ceiling.

  torn records (finding 4): a torn-shaped record tolerated by the V2 rules
      (mid-write reset: hum+press erased, position attributable) carries its
      previous record's hum/press forward instead of the raw 0xFFFFFFFF, which
      would otherwise normalize to ~4e5 and dominate reconstruction error --
      the AE must not re-introduce the false positive the rules just fixed.
      A NON-tolerated torn-shaped record keeps its raw values: visible anomaly.
"""
from __future__ import annotations

import numpy as np

from offdevice.nv import spec
from offdevice.nv.parse import PageView, parse_region, slice_nv

GRID_ROWS = spec.RECORDS_TOTAL          # 244
GRID_COLS = 5                           # ts_delta, temp, hum, press, present
GRID_SHAPE = (GRID_ROWS, GRID_COLS)

_PERIOD = spec.RATE_DEPLOY_PERIOD_S     # 15 s
_DT_KNEE = 4.0                          # linear up to here, log-compressed above
_DT_CAP = 8.0
_SEAM = -1.0                            # benign boot-reset landing
_REGRESSION = -2.0                      # mid-stream non-increasing step

# Boot-seam landing window: the first record after a reboot carries ts ~= PERIOD
# (the logger's first sample fires one period after boot); the slack absorbs
# boot/sensor warm-up. Benign landings in the 153-capture bank are all exactly
# 15; the one real mid-stream regression lands at 2131.
LANDING_LIMIT_S = spec.RATE_DEPLOY_PERIOD_S * 2   # 30

_ERASED_U32 = int.from_bytes(bytes([spec.ERASED_BYTE]) * 4, "little")


def _is_torn_shaped(rec: dict[str, int]) -> bool:
    """Both fields of the record's second doubleword read erased."""
    return rec["hum"] == _ERASED_U32 and rec["press"] == _ERASED_U32


def _torn_tolerated(page_recs: tuple[dict[str, int], ...], i: int) -> bool:
    """Torn-shaped record i of one page is attributable to a mid-write reset.

    The intact first doubleword must be plausible (temp in range) and the slot
    must sit where a reset can leave it: the last written slot of its page, or
    immediately before a boot-reset landing. Consecutive torn slots chain
    naturally: each is followed by a torn/seam successor or the page end.
    """
    rec = page_recs[i]
    temp_ch = spec.CHANNELS[0]
    if not (temp_ch.lo <= rec["temp"] <= temp_ch.hi):
        return False
    if i == len(page_recs) - 1:
        return True
    nxt = page_recs[i + 1]
    return _is_torn_shaped(nxt) or nxt["ts"] <= LANDING_LIMIT_S


def _page_torn_map(page: PageView) -> tuple[bool, ...]:
    """Per record of one page: True = torn-shaped AND tolerated (benign)."""
    return tuple(
        _is_torn_shaped(r) and _torn_tolerated(page.records, i)
        for i, r in enumerate(page.records)
    )


def _encode_dt(ts: int, prev_ts: int | None) -> float:
    if prev_ts is None:
        return 1.0
    dt = (ts - prev_ts) / _PERIOD
    if dt <= 0.0:
        return _SEAM if ts <= LANDING_LIMIT_S else _REGRESSION
    if dt <= _DT_KNEE:
        return float(dt)
    return float(min(_DT_KNEE + np.log2(dt / _DT_KNEE), _DT_CAP))


def nv_grid_v2(nv: bytes) -> np.ndarray:
    """4 KB NV region (or full 256 KB dump) -> (RECORDS_TOTAL, 5) float32 grid."""
    if len(nv) == spec.DUMP_OFFSET + spec.REGION_SIZE:
        nv = slice_nv(nv)
    view = parse_region(nv)

    ordered: list[tuple[dict[str, int], bool]] = []
    if view.current is not None:
        other = 1 - view.current
        pages = ([view.pages[other]] if view.pages[other].header is not None else [])
        pages.append(view.pages[view.current])
        for page in pages:
            ordered.extend(zip(page.records, _page_torn_map(page)))

    grid = np.zeros(GRID_SHAPE, dtype=np.float32)
    ch = {c.name: c for c in spec.CHANNELS}

    def norm(v: float, c) -> float:
        return (v - c.lo) / (c.hi - c.lo)

    prev_ts: int | None = None
    prev_hum: float | None = None
    prev_press: float | None = None
    for i, (r, tolerated) in enumerate(ordered[:GRID_ROWS]):
        grid[i, 0] = _encode_dt(r["ts"], prev_ts)
        prev_ts = r["ts"]
        grid[i, 1] = norm(r["temp"], ch["temp"])
        if _is_torn_shaped(r) and tolerated:
            # carry the neighborhood forward: channels are smooth at 15 s cadence,
            # so the previous values are reconstruction-friendly stand-ins
            grid[i, 2] = prev_hum if prev_hum is not None else 0.5
            grid[i, 3] = prev_press if prev_press is not None else 0.5
        else:
            grid[i, 2] = norm(r["hum"], ch["hum"])
            grid[i, 3] = norm(r["press"], ch["press"])
        prev_hum, prev_press = float(grid[i, 2]), float(grid[i, 3])
        grid[i, 4] = 1.0
    return grid
