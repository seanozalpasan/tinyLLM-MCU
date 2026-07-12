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
    # 64 + 32 + 122*16 == 2048: every byte of a page is spec-defined (header,
    # journal slot, record, or blank slot) -- no unspecified tail for a payload
    # to hide in.
    assert (spec.HEADER_SIZE + spec.JOURNAL_SIZE
            + spec.RECORDS_PER_PAGE * spec.RECORD_SIZE == spec.PAGE_SIZE)
    assert spec.RECORDS_PER_PAGE == 122
    assert spec.RECORDS_TOTAL == 244


def test_journal_geometry() -> None:
    # The journal sits between header and records at the design-record offsets;
    # one entry == one flash doubleword is the atomicity guarantee (a reset
    # mid-write leaves a slot fully written or still blank, never half-written).
    assert spec.SPEC_VERSION == 2
    assert spec.JOURNAL_OFFSET == 0x040
    assert spec.JOURNAL_SLOTS == 4
    assert spec.JOURNAL_ENTRY_SIZE == spec.DOUBLEWORD
    assert spec.JOURNAL_SIZE == 32
    assert spec.RECORDS_OFFSET == 0x060


def test_display_units_pinned() -> None:
    # 0 must stay the canonical default (the encoding records are stored in);
    # the parser and the benign gate check unit fields against {0, 1}.
    assert spec.UNIT_TEMP_C == 0 and spec.UNIT_TEMP_F == 1
    assert spec.UNIT_PRESS_HPA == 0 and spec.UNIT_PRESS_INHG == 1


def test_blank_journal_slot_never_parses_as_an_entry() -> None:
    # A never-written slot reads all 0xFF: reserved0 then reads 0xFFFF, so the
    # "reserved0 must be 0" rule makes blank unmistakable for a real entry.
    fields = dict(zip(spec.JOURNAL_FIELDS,
                      struct.unpack(spec.JOURNAL_FMT, spec.BLANK_JOURNAL_ENTRY)))
    assert fields["reserved0"] != 0


def test_strides_are_doubleword_multiples() -> None:
    # The L5 programs 8 B doublewords that can't be reprogrammed; a slot sharing
    # a doubleword with its neighbor would make the second write impossible.
    assert spec.HEADER_SIZE % spec.DOUBLEWORD == 0
    assert spec.JOURNAL_ENTRY_SIZE % spec.DOUBLEWORD == 0
    assert spec.RECORD_SIZE % spec.DOUBLEWORD == 0


def test_struct_formats_match_declared_sizes() -> None:
    assert struct.calcsize(spec.HEADER_FMT) == spec.HEADER_SIZE
    assert struct.calcsize(spec.JOURNAL_FMT) == spec.JOURNAL_ENTRY_SIZE
    assert struct.calcsize(spec.RECORD_FMT) == spec.RECORD_SIZE
    assert spec.HEADER_PAD >= 0
    n_header = len(struct.unpack(spec.HEADER_FMT, bytes(spec.HEADER_SIZE)))
    n_journal = len(struct.unpack(spec.JOURNAL_FMT, bytes(spec.JOURNAL_ENTRY_SIZE)))
    n_record = len(struct.unpack(spec.RECORD_FMT, bytes(spec.RECORD_SIZE)))
    assert n_header == len(spec.HEADER_FIELDS)
    assert n_journal == len(spec.JOURNAL_FIELDS)
    assert n_record == len(spec.RECORD_FIELDS)


def test_blank_sentinels() -> None:
    assert spec.BLANK_HEADER == b"\xff" * spec.HEADER_SIZE
    assert spec.BLANK_JOURNAL_ENTRY == b"\xff" * spec.JOURNAL_ENTRY_SIZE
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
    # coverage + flash endurance), not a refactor. 15 s trades endurance
    # (~14 months to rated wear) for dataset yield (ring turnover ~61 min --
    # a ~115-capture campaign fits in ~3 days).
    assert spec.RATE_DEV_PERIOD_S == 1
    assert spec.RATE_DEPLOY_PERIOD_S == 15


@pytest.mark.parametrize("target", TARGETS, ids=lambda p: p.parts[-4])
def test_generated_headers_are_current(target: Path) -> None:
    if not target.exists():
        pytest.fail(f"{target} missing -- run: python -m offdevice.nv.gen_header")
    assert target.read_text() == render(), (
        f"{target} drifted from spec.py -- regenerate, never hand-edit"
    )
