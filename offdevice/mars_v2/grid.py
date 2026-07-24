"""
This turns the 4 KB NV region into a (244, 5) grid the CNN can look at.
One row per record slot, five columns: ts_delta, temp, hum, press, present.

How the timestamp column works: records store seconds since boot, so a reboot
makes the timestamp start over which is normal, not tampering. 

The encoding keeps the two cases apart:
    -   a backwards step that lands at or under 30 s  -> -1.0 (normal reboot seam)
    -   a backwards step that lands anywhere else     -> -2.0 (someone rewrote time)
    -   forward gaps count up linearly to 4 periods, then get log-compressed and
        capped at 8, so a huge gap still stands out instead of blending in

Torn records: if the device resets in the middle of writing a record, the
second half (hum + press) reads erased. When the torn slot sits where a reset
can actually leave it, we carry the previous record's values forward so the
model does not panic over normal wear. A torn slot anywhere else keeps its raw
erased values and stays visible as suspicious.
"""
from __future__ import annotations

import numpy as np

from offdevice.nv import spec
from offdevice.nv.parse import PageView, parse_region, slice_nv

GRID_ROWS = spec.RECORDS_TOTAL          # 244 record slots across both pages
GRID_COLS = 5                           # ts_delta, temp, hum, press, present
GRID_SHAPE = (GRID_ROWS, GRID_COLS)

_PERIOD = spec.RATE_DEPLOY_PERIOD_S     # 15 s between records
_DT_KNEE = 4.0                          # linear up to here, log-compressed above
_DT_CAP = 8.0
_SEAM = -1.0                            # normal reboot landing
_REGRESSION = -2.0                      # rewritten / backwards time

# The first record after a reboot carries ts of about one period (the logger's
# first sample fires one period after boot); doubling it gives slack for boot
# and sensor warm-up. Every normal reboot in our capture bank lands at exactly
# 15 s; the one real tampered timestamp we have landed at 2131 s.
LANDING_LIMIT_S = spec.RATE_DEPLOY_PERIOD_S * 2   # 30 s

_ERASED_U32 = int.from_bytes(bytes([spec.ERASED_BYTE]) * 4, "little")


def _is_torn_shaped(record: dict[str, int]) -> bool:
    """True when the record's second half (hum + press) reads erased."""
    return record["hum"] == _ERASED_U32 and record["press"] == _ERASED_U32


def _torn_tolerated(page_records: tuple[dict[str, int], ...],
                    slot_index: int) -> bool:
    """Is this torn record explainable by a reset during the write?

    The intact first half has to look plausible (temp in range), and the slot
    has to sit where a reset can actually leave one: the last written slot of
    the page, or right before a reboot landing. Back-to-back torn slots chain
    naturally: each one is followed by another torn slot, a reboot seam, or
    the end of the page.
    """
    record = page_records[slot_index]
    temp_channel = spec.CHANNELS[0]
    if not (temp_channel.lo <= record["temp"] <= temp_channel.hi):
        return False
    if slot_index == len(page_records) - 1:
        return True
    next_record = page_records[slot_index + 1]
    return _is_torn_shaped(next_record) or next_record["ts"] <= LANDING_LIMIT_S


def _page_torn_map(page: PageView) -> tuple[bool, ...]:
    """Per record of one page: True = torn AND explainable by a reset (benign)."""
    return tuple(
        _is_torn_shaped(record) and _torn_tolerated(page.records, slot_index)
        for slot_index, record in enumerate(page.records)
    )


def _encode_dt(timestamp: int, previous_timestamp: int | None) -> float:
    if previous_timestamp is None:
        return 1.0
    delta = (timestamp - previous_timestamp) / _PERIOD
    if delta <= 0.0:
        return _SEAM if timestamp <= LANDING_LIMIT_S else _REGRESSION
    if delta <= _DT_KNEE:
        return float(delta)
    return float(min(_DT_KNEE + np.log2(delta / _DT_KNEE), _DT_CAP))


def nv_grid_v2(nv: bytes) -> np.ndarray:
    """4 KB NV region (or full 256 KB dump) -> (244, 5) float32 grid."""
    if len(nv) == spec.DUMP_OFFSET + spec.REGION_SIZE:
        nv = slice_nv(nv)
    view = parse_region(nv)

    # records oldest-first: the older page's records, then the current page's
    records_in_order: list[tuple[dict[str, int], bool]] = []
    if view.current is not None:
        other = 1 - view.current
        pages = ([view.pages[other]] if view.pages[other].header is not None else [])
        pages.append(view.pages[view.current])
        for page in pages:
            records_in_order.extend(zip(page.records, _page_torn_map(page)))

    grid = np.zeros(GRID_SHAPE, dtype=np.float32)
    channel_by_name = {channel.name: channel for channel in spec.CHANNELS}

    def normalize(value: float, channel) -> float:
        return (value - channel.lo) / (channel.hi - channel.lo)

    previous_timestamp: int | None = None
    previous_humidity: float | None = None
    previous_pressure: float | None = None
    for row, (record, tolerated) in enumerate(records_in_order[:GRID_ROWS]):
        grid[row, 0] = _encode_dt(record["ts"], previous_timestamp)
        previous_timestamp = record["ts"]
        grid[row, 1] = normalize(record["temp"], channel_by_name["temp"])
        if _is_torn_shaped(record) and tolerated:
            # carry the neighborhood forward: readings are smooth at a 15 s
            # cadence, so the previous values are believable stand-ins
            grid[row, 2] = previous_humidity if previous_humidity is not None else 0.5
            grid[row, 3] = previous_pressure if previous_pressure is not None else 0.5
        else:
            grid[row, 2] = normalize(record["hum"], channel_by_name["hum"])
            grid[row, 3] = normalize(record["press"], channel_by_name["press"])
        previous_humidity = float(grid[row, 2])
        previous_pressure = float(grid[row, 3])
        grid[row, 4] = 1.0
    return grid
