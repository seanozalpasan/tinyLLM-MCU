"""
Record-GRID features for the 4 KB NV region -- the "change the representation"
answer to the aggregation ceiling.

Every aggregate feature tried today (MFCC time-average, region-wide correlation,
pooled physics fit) diluted a localized tamper into nothing: 13 changed records
out of 244 vanish under a mean. This keeps the records SEPARATE -- one row per
slot -- so a convolution slides over the record axis and a local anomaly stays a
local pattern.

    parse the current page chain -> (RECORDS_TOTAL, 5) float grid, one row =
        [ts_delta, temp, hum, press, present]
    channels normalized by the SPEC's declared lo/hi (not data statistics), so an
        out-of-range value lands outside [0,1] and stays visible to the model.
    ts_delta = seconds since the previous record, /RATE so a normal cadence ~1.0;
        a backwards timestamp goes negative -- the nonmonotonic_ts signal.
    present = 1 for a real record, 0 for the zero-padded tail.

Grid, not vector: feeds a Conv autoencoder (model.build_grid_autoencoder).
"""
from __future__ import annotations

import numpy as np

from offdevice.nv import spec
from offdevice.nv.parse import parse_region, records_chronological, slice_nv

GRID_ROWS = spec.RECORDS_TOTAL          # 244
GRID_COLS = 5                           # ts_delta, temp, hum, press, present
GRID_SHAPE = (GRID_ROWS, GRID_COLS)

# nominal record period; a benign ts_delta divides to ~1.0
_PERIOD = spec.RATE_DEPLOY_PERIOD_S      # 15 s


def _norm(v, lo, hi):
    """Map [lo, hi] -> [0, 1]; out-of-range stays outside so the model sees it."""
    return (v - lo) / (hi - lo)


def nv_grid(nv: bytes) -> np.ndarray:
    """4 KB NV region (or full 256 KB dump) -> (RECORDS_TOTAL, 5) float32 grid."""
    if len(nv) == spec.DUMP_OFFSET + spec.REGION_SIZE:
        nv = slice_nv(nv)
    recs = records_chronological(parse_region(nv))

    grid = np.zeros(GRID_SHAPE, dtype=np.float32)
    ch = {c.name: c for c in spec.CHANNELS}
    prev_ts = None
    for i, r in enumerate(recs[:GRID_ROWS]):
        dt = 1.0 if prev_ts is None else (r["ts"] - prev_ts) / _PERIOD
        prev_ts = r["ts"]
        # clip: benign cadence ~1; large gaps / page-boundary jumps (ts is
        # seconds-since-boot, not comparable across a boot) saturate at 4 instead
        # of dominating the AE; a backwards ts (nonmonotonic_ts attack) stays
        # negative and distinct.
        grid[i, 0] = float(np.clip(dt, -2.0, 4.0))
        grid[i, 1] = _norm(r["temp"], ch["temp"].lo, ch["temp"].hi)
        grid[i, 2] = _norm(r["hum"], ch["hum"].lo, ch["hum"].hi)
        grid[i, 3] = _norm(r["press"], ch["press"].lo, ch["press"].hi)
        grid[i, 4] = 1.0
    return grid


if __name__ == "__main__":
    import sys
    from pathlib import Path

    for p in sys.argv[1:]:
        g = nv_grid(Path(p).read_bytes())
        n = int(g[:, 4].sum())
        print(f"{Path(p).name}: grid {g.shape}, {n} records, "
              f"ts_delta[min={g[:n,0].min():.2f} max={g[:n,0].max():.2f}] "
              f"temp[{g[:n,1].min():.3f}..{g[:n,1].max():.3f}]")
