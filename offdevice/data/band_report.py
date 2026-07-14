"""
Band-coverage report over a folder of saved captures: how many sit in the
steep 238..244-record band (and just after the page rotation), and how many
the campaign still owes against the sizing target.

The one-class fit's weakest region is the ring-cycle extremes -- the largest
benign distances all sit at >= 238 records or just past a page rotation, and
the ring spends only ~5% of each cycle there, so uniform sampling starves the
band. This report is the check that dedicated chain captures (or a short
repair run) actually banked it. Read-only, and dependency-light on purpose
(stdlib + the NV parser): it runs against any folder of captures on any
machine -- the live collection desktop, a zip extract, or the merged laptop
set -- without the model stack installed.

Run (repo root):
    python -m offdevice.data.band_report
    python -m offdevice.data.band_report <folder> --target 5
Exits 1 while the band is short of --target, so scripts can gate on it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from offdevice.nv import spec
from offdevice.nv.parse import DUMP_SIZE, parse_region, slice_nv

# Same derivations as campaign.py's schedule constants (not imported from
# there: campaign.py needs pyserial, and this report must run anywhere).
BAND_LO = spec.RECORDS_TOTAL - 6
BAND_HI = spec.RECORDS_TOTAL
ROTATION_LO = spec.RECORDS_PER_PAGE + 1
ROTATION_HI = spec.RECORDS_PER_PAGE + 8

# Mirrors capture.py's default (not imported: same pyserial reason).
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAPTURES_DIR = REPO_ROOT / "offdevice" / "data" / "captures"


def scan_capture(path: Path) -> tuple[int, str]:
    """(record total, note) for one capture; total -1 = not a parsable capture.

    Only pages with a valid header count records: a foreign page (spec-v1
    leftovers, corruption) parses to garbage slot counts that could land
    "in-band" by accident and inflate the coverage number.
    """
    data = path.read_bytes()
    if len(data) == spec.REGION_SIZE:
        nv = data
    elif len(data) == DUMP_SIZE:
        nv = slice_nv(data)
    else:
        return -1, f"not a {DUMP_SIZE}-byte dump or a bare region ({len(data)} B) -- skipped"
    rv = parse_region(nv)
    total = sum(len(p.records) for p in rv.pages if p.header is not None)
    if any(p.header is None and not p.blank for p in rv.pages):
        return total, "FOREIGN page (bytes present, no valid header)"
    return total, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Count captures in the 238..244-record band vs the sizing target.")
    ap.add_argument("folder", nargs="?", type=Path, default=DEFAULT_CAPTURES_DIR,
                    help="dir of .bin captures (default: the repo captures dir)")
    ap.add_argument("--target", type=int, default=5,
                    help="minimum in-band captures owed (default 5 = the sizing floor)")
    args = ap.parse_args(argv)

    bins = sorted(args.folder.glob("*.bin"))
    if not bins:
        print(f"[band] no .bin captures under {args.folder}")
        return 1

    rows: list[tuple[int, str, str]] = []
    for path in bins:
        total, note = scan_capture(path)
        if total < 0:
            print(f"[band] {path.name}: {note}")
            continue
        rows.append((total, path.name, note))

    in_band = rotation = 0
    for total, name, note in sorted(rows):
        mark = ""
        if BAND_LO <= total <= BAND_HI:
            mark = "IN-BAND"
            in_band += 1
        elif ROTATION_LO <= total <= ROTATION_HI:
            mark = "rotation"
            rotation += 1
        suffix = f"  !! {note}" if note else ""
        print(f"[band] {total:>4} recs  {mark:<8} {name}{suffix}")

    missing = max(0, args.target - in_band)
    print(f"[band] {len(rows)} captures: {in_band} in the {BAND_LO}..{BAND_HI} band, "
          f"{rotation} just-after-rotation ({ROTATION_LO}..{ROTATION_HI})")
    print(f"[band] target >={args.target} in-band: "
          + (f"MISSING {missing}" if missing else "met"))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
