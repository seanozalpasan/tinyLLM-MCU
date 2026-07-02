"""
Unit tests for the NV byte spec: layout math, struct/field agreement, channel
ranges, and regen discipline (the generated firmware headers match spec.py).

Run from the repo root (so `import offdevice...` resolves):
    pytest offdevice\\tests\\test_nv_spec.py -v
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from offdevice.nv import spec
from offdevice.nv.gen_header import TARGETS, render


def test_region_geometry() -> None:
    assert spec.REGION_BASE == 0x0807F000
    assert spec.REGION_SIZE == 4096
    assert spec.NUM_PAGES == 2
    assert spec.PAGE_BASES == (0x0807F000, 0x0807F800)
    assert spec.REGION_BASE + spec.REGION_SIZE - 1 == 0x0807FFFF
    assert spec.DUMP_OFFSET == 0x3F000
    assert spec.STATIC_SIZE == 0x3F000


def test_page_layout_fills_exactly() -> None:
    # 64 + 124*16 == 2048: every byte of a page is spec-defined (header, record,
    # or blank slot) -- no unspecified tail for a payload to hide in.
    assert spec.HEADER_SIZE + spec.RECORDS_PER_PAGE * spec.RECORD_SIZE == spec.PAGE_SIZE
    assert spec.RECORDS_PER_PAGE == 124
    assert spec.RECORDS_TOTAL == 248


def test_strides_are_doubleword_multiples() -> None:
    # The L5 programs 8 B doublewords that can't be reprogrammed; a slot sharing
    # a doubleword with its neighbor would make the second write impossible.
    assert spec.HEADER_SIZE % spec.DOUBLEWORD == 0
    assert spec.RECORD_SIZE % spec.DOUBLEWORD == 0


def test_struct_formats_match_declared_sizes() -> None:
    assert struct.calcsize(spec.HEADER_FMT) == spec.HEADER_SIZE
    assert struct.calcsize(spec.RECORD_FMT) == spec.RECORD_SIZE
    assert spec.HEADER_PAD >= 0
    n_header = len(struct.unpack(spec.HEADER_FMT, bytes(spec.HEADER_SIZE)))
    n_record = len(struct.unpack(spec.RECORD_FMT, bytes(spec.RECORD_SIZE)))
    assert n_header == len(spec.HEADER_FIELDS)
    assert n_record == len(spec.RECORD_FIELDS)


def test_blank_sentinels() -> None:
    assert spec.BLANK_HEADER == b"\xff" * spec.HEADER_SIZE
    assert spec.BLANK_RECORD == b"\xff" * spec.RECORD_SIZE


def test_channel_ranges_fit_their_fields() -> None:
    for ch in spec.CHANNELS:
        lo_limit, hi_limit = (-(2**31), 2**31 - 1) if ch.fmt.islower() else (0, 2**32 - 1)
        assert lo_limit <= ch.lo < ch.hi <= hi_limit, ch.name
        assert ch.scale > 0, ch.name


def test_channel_ranges_are_bme280() -> None:
    by_name = {ch.name: ch for ch in spec.CHANNELS}
    assert (by_name["temp"].lo, by_name["temp"].hi) == (-4_000, 8_500)
    assert (by_name["hum"].lo, by_name["hum"].hi) == (0, 10_000)
    assert (by_name["press"].lo, by_name["press"].hi) == (30_000, 110_000)


def test_rate_presets_pinned() -> None:
    # Deliberately pinned: changing a preset is a data-plan decision (benign
    # coverage + flash endurance), not a refactor.
    assert spec.RATE_DEV_PERIOD_S == 1
    assert spec.RATE_DEPLOY_PERIOD_S == 90


@pytest.mark.parametrize("target", TARGETS, ids=lambda p: p.parts[-4])
def test_generated_headers_are_current(target: Path) -> None:
    if not target.exists():
        pytest.fail(f"{target} missing -- run: python -m offdevice.nv.gen_header")
    assert target.read_text() == render(), (
        f"{target} drifted from spec.py -- regenerate, never hand-edit"
    )
