"""Deterministic synthetic input for tests -- no external/real data.

One closed-form, spec-conformant 4 KB NV image: valid page headers, records on
their 16 B stride with in-range triangle-wave values, a monotonic timestamp, and
a blank (0xFF) tail -- the byte regime the re-tuned features actually see, unlike
random bytes. NOT training data. Every constant below is FROZEN -- changing any
of them invalidates the golden vector (offdevice/tests/make_golden.py).
"""

import struct

from offdevice.nv import spec

# Steady-state fill: page0 full (older), page1 partial with a blank tail -- the
# state a deployed ring lives in. seq/boot/op are mutually consistent per the
# header semantics (op_count = records programmed before the page opened).
PAGE0_SEQ, PAGE1_SEQ = 3, 4
BOOT_COUNT = 2
PAGE0_OP = 2 * spec.RECORDS_PER_PAGE          # two prior page-fills
PAGE1_OP = PAGE0_OP + spec.RECORDS_PER_PAGE
PAGE1_RECORDS = 60

TS_FIRST = 1000     # seconds since boot at the oldest record
TS_STEP = 45        # == the deploy-rate record period

# Triangle-wave value generators (mid, amplitude, period-in-records); hum is
# anti-correlated with temp, mirroring the firmware's dummy generator shape.
TEMP_MID, TEMP_AMP, TEMP_PER = 2350, 400, 25
HUM_MID = 4500
PRESS_MID, PRESS_AMP, PRESS_PER = 101300, 60, 15

# Header stats: the waves' closed-form min/max/mean (constants, not computed --
# the parser doesn't validate them; they just have to be in-range and plausible).
_STATS = (TEMP_MID - TEMP_AMP, TEMP_MID + TEMP_AMP, TEMP_MID,
          HUM_MID - TEMP_AMP // 2, HUM_MID + TEMP_AMP // 2, HUM_MID,
          PRESS_MID - PRESS_AMP, PRESS_MID + PRESS_AMP, PRESS_MID)


def _tri(i: int, period: int, amp: int) -> int:
    """Integer triangle wave over record index i: -amp at phase 0, +amp at period."""
    phase = i % (2 * period)
    up = period - abs(phase - period)
    return (2 * up - period) * amp // period


def _header(page_seq: int, op_count: int) -> bytes:
    return struct.pack(spec.HEADER_FMT, spec.SPEC_VERSION, 0,
                       page_seq, BOOT_COUNT, op_count, *_STATS)


def _record(i: int) -> bytes:
    temp = TEMP_MID + _tri(i, TEMP_PER, TEMP_AMP)
    hum = HUM_MID - _tri(i, TEMP_PER, TEMP_AMP) // 2
    press = PRESS_MID + _tri(i, PRESS_PER, PRESS_AMP)
    return struct.pack(spec.RECORD_FMT, TS_FIRST + TS_STEP * i, temp, hum, press)


def synthetic_nv_region() -> bytes:
    """Return the deterministic 4 KB NV image used as the golden-vector input."""
    page0 = _header(PAGE0_SEQ, PAGE0_OP) + b"".join(
        _record(i) for i in range(spec.RECORDS_PER_PAGE))
    page1 = _header(PAGE1_SEQ, PAGE1_OP) + b"".join(
        _record(spec.RECORDS_PER_PAGE + i) for i in range(PAGE1_RECORDS))
    page1 += bytes([spec.ERASED_BYTE]) * (spec.PAGE_SIZE - len(page1))
    region = page0 + page1
    assert len(region) == spec.REGION_SIZE
    return region
