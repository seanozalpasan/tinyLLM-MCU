"""
NV byte spec -- contract #1: the single source of truth for the 4 KB NV layout.

The firmware logger writes this layout and the Python reader parses it,
byte-for-byte, or the model trains on one distribution and infers on another.
The C side never re-types these numbers: `python -m offdevice.nv.gen_header`
renders nv_spec.h into both firmware projects from the constants below. Change
something here -> regenerate -> rebuild; never edit the generated headers.

Layout, per 2 KB flash page (little-endian throughout; two pages = the region):

    [header 64 B][124 x 16 B records]

The header is programmed once each time a page is erased+reopened ("page-open");
records then append into fixed 16 B slots. A slot is two 8-byte flash
doublewords -- the L5's program granularity, un-rewritable without a page erase,
so nothing in flash is ever updated in place: counters and stats live in RAM and
are snapshotted into the header at page-open, and the write head is FOUND (first
all-0xFF slot of the newest page), never stored. Newest page = highest page_seq.
"""

import struct
from typing import NamedTuple

SPEC_VERSION = 1   # bump on ANY layout or semantics change

# ---- region geometry (fixed by the locked TrustZone partition) -----------------

NS_FLASH_BASE = 0x08040000   # NS Bank-2 base == flash_dump origin
REGION_BASE = 0x0807F000     # top two 2 KB pages of Bank 2 (pages 126/127)
REGION_SIZE = 0x1000
PAGE_SIZE = 0x800
NUM_PAGES = REGION_SIZE // PAGE_SIZE
PAGE_BASES = tuple(REGION_BASE + i * PAGE_SIZE for i in range(NUM_PAGES))
DUMP_OFFSET = REGION_BASE - NS_FLASH_BASE   # where the region sits in a 256 KB dump
STATIC_SIZE = DUMP_OFFSET    # Part-1 hash covers [NS_FLASH_BASE, REGION_BASE)

DOUBLEWORD = 8               # flash program granularity; every stride is a multiple
ERASED_BYTE = 0xFF           # an un-programmed flash byte reads as this


# ---- channels -------------------------------------------------------------------
# Values are fixed-point INTEGERS -- floats inject NaN/denormal byte patterns that
# widen the benign byte distribution, and a higher noise floor helps a payload
# hide. Ranges are the BME280's measurement ranges (the AITRIP breakout carries
# the same die as the Adafruit board), so the real-sensor swap changes nothing.

class Channel(NamedTuple):
    """One channel's wire encoding: C/struct type, fixed-point scale, legal range."""

    name: str
    c_type: str     # generated-header field type
    fmt: str        # struct format char (same width + signedness as c_type)
    unit: str       # physical unit before the fixed-point scale is applied
    scale: int      # stored value = physical value x scale
    lo: int         # smallest legal stored value
    hi: int         # largest legal stored value
    note: str = ""  # extra unit context for the generated header


CHANNELS = (
    Channel("temp", "int32_t", "i", "degC", 100, -4_000, 8_500),
    Channel("hum", "uint32_t", "I", "%RH", 100, 0, 10_000),
    Channel("press", "uint32_t", "I", "hPa", 100, 30_000, 110_000, note="== Pa"),
)


# ---- page header (64 B, programmed once per page-open) ---------------------------
# Nothing in it is live -- flash can't rewrite in place:
#   version     spec version, for parse-time compatibility checks
#   page_seq    monotonic page-open counter, 1 on virgin flash; doubles as the
#               wrap counter, and the highest one marks the current page
#   boot_count  boots as of this page-open (RAM-kept: newest header's value + 1
#               at boot, so a dump shows the count as of the last page-open)
#   op_count    records fully programmed BEFORE this page opened; lifetime total
#               = op_count + the page's non-blank slots
#   <ch>_{min,max,mean}  per-channel stats over all readings THIS boot, including
#               the reading whose record triggered the page-open; mean = 64-bit
#               sum / count, truncated toward zero (C99 division)

HEADER_SIZE = 64
HEADER_PAD_FILL = 0x00   # trailing reserve is programmed as zeros, not left 0xFF
HEADER_STATS = ("min", "max", "mean")
HEADER_FIELDS = (
    "version",
    "reserved0",
    "page_seq",
    "boot_count",
    "op_count",
) + tuple(f"{ch.name}_{stat}" for ch in CHANNELS for stat in HEADER_STATS)
_HEADER_FMT_NO_PAD = "<HHIII" + "".join(ch.fmt * len(HEADER_STATS) for ch in CHANNELS)
HEADER_PAD = HEADER_SIZE - struct.calcsize(_HEADER_FMT_NO_PAD)
HEADER_FMT = _HEADER_FMT_NO_PAD + f"{HEADER_PAD}x"
BLANK_HEADER = bytes([ERASED_BYTE]) * HEADER_SIZE   # a never-opened page starts so


# ---- records (16 B each, appended after the header until the page is full) -------
# ts = u32 SECONDS since boot: monotonic within a boot (boot_count separates
# boots) and never wraps in practice; milliseconds would wrap in 49.7 days and
# mimic the exact non-monotonic-timestamp anomaly the detector hunts.

RECORD_FIELDS = ("ts",) + tuple(ch.name for ch in CHANNELS)
RECORD_FMT = "<I" + "".join(ch.fmt for ch in CHANNELS)
RECORD_SIZE = struct.calcsize(RECORD_FMT)
RECORDS_PER_PAGE = (PAGE_SIZE - HEADER_SIZE) // RECORD_SIZE
RECORDS_TOTAL = NUM_PAGES * RECORDS_PER_PAGE
BLANK_RECORD = bytes([ERASED_BYTE]) * RECORD_SIZE   # an unwritten slot reads as this


# ---- update-rate presets (period between records, seconds) -----------------------
# The rate is an operating knob, not layout: flash ages by ERASE COUNT (pages are
# rated ~10k cycles minimum; each page erases once per RECORDS_TOTAL records). At
# 1 s the ring wraps in ~4 min -- bring-up only, never training data. At 45 s each
# page erases every ~3.1 h => ~3.5 years to the rated minimum, and a fully fresh
# benign snapshot exists every ring turnover (~3.1 h) -- the balance between a
# credible device lifetime and dataset accumulation speed. The model trains at
# the deploy rate only (train == infer distribution).

RATE_DEV_PERIOD_S = 1
RATE_DEPLOY_PERIOD_S = 45
