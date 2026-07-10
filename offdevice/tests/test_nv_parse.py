"""
Unit tests for the NV reader: synthetic pages round-trip through parse exactly,
foreign/blank pages are rejected, and layout violations are flagged not repaired.

Run from the repo root (so `import offdevice...` resolves):
    pytest offdevice\\tests\\test_nv_parse.py -v
"""

from __future__ import annotations

import struct

import pytest

from offdevice.nv import spec
from offdevice.nv.parse import (
    DUMP_SIZE,
    journal_chain,
    parse_header,
    parse_page,
    parse_region,
    records_chronological,
    slice_nv,
)


# ---- synthetic builders (the firmware writer, in miniature) ----------------------

def make_header(**over: int) -> bytes:
    fields = dict.fromkeys(spec.HEADER_FIELDS, 0)
    fields.update(version=spec.SPEC_VERSION, page_seq=1, boot_count=1)
    fields.update(over)
    return struct.pack(spec.HEADER_FMT, *(fields[f] for f in spec.HEADER_FIELDS))


def make_record(ts: int, temp: int, hum: int, press: int) -> bytes:
    return struct.pack(spec.RECORD_FMT, ts, temp, hum, press)


def make_journal_entry(**over: int) -> bytes:
    fields = dict.fromkeys(spec.JOURNAL_FIELDS, 0)
    fields.update(over)
    return struct.pack(spec.JOURNAL_FMT, *(fields[f] for f in spec.JOURNAL_FIELDS))


def make_page(header: bytes | None, records: list[bytes],
              journal: list[bytes] | None = None) -> bytes:
    """journal = written slots from J0 on; the rest stay blank (all 0xFF)."""
    slots = journal if journal is not None else []
    assert len(slots) <= spec.JOURNAL_SLOTS
    assert len(records) <= spec.RECORDS_PER_PAGE
    jbody = b"".join(slots) + spec.BLANK_JOURNAL_ENTRY * (spec.JOURNAL_SLOTS - len(slots))
    body = b"".join(records)
    page = ((header if header is not None else spec.BLANK_HEADER) + jbody + body
            + spec.BLANK_RECORD * (spec.RECORDS_PER_PAGE - len(records)))
    assert len(page) == spec.PAGE_SIZE
    return page


def test_blank_region_parses_empty() -> None:
    view = parse_region(b"\xff" * spec.REGION_SIZE)
    assert view.current is None
    assert all(p.header is None and p.records == () and p.tail_clean and p.blank
               and p.pad_clean for p in view.pages)
    assert all(p.journal == (None,) * spec.JOURNAL_SLOTS and p.journal_tail_clean
               for p in view.pages)
    assert records_chronological(view) == ()


def test_round_trip_two_page_ring() -> None:
    # Page 0 = the older, full page (seq 1); page 1 = current, partially filled (seq 2).
    old_recs = [make_record(t, -100 + t, 4500 + t, 101325 + t) for t in range(spec.RECORDS_PER_PAGE)]
    new_recs = [make_record(t, 2200 + t, 4400, 101300) for t in range(5)]
    nv = (make_page(make_header(page_seq=1, op_count=0), old_recs,
                    journal=[make_journal_entry(),
                             make_journal_entry(unit_temp=1, op_count=40)])
          + make_page(make_header(page_seq=2, boot_count=2, op_count=spec.RECORDS_PER_PAGE),
                      new_recs,
                      journal=[make_journal_entry(unit_temp=1,
                                                  op_count=spec.RECORDS_PER_PAGE)]))

    view = parse_region(nv)
    assert view.current == 1
    assert view.pages[0].header is not None and view.pages[0].header["page_seq"] == 1
    assert (view.pages[1].header is not None
            and view.pages[1].header["op_count"] == spec.RECORDS_PER_PAGE)
    assert len(view.pages[0].records) == spec.RECORDS_PER_PAGE
    assert len(view.pages[1].records) == 5
    assert view.pages[1].records[3] == {"ts": 3, "temp": 2203, "hum": 4400, "press": 101300}
    assert view.pages[0].journal[1] == {"unit_temp": 1, "unit_press": 0,
                                        "reserved0": 0, "op_count": 40}
    assert view.pages[0].journal[2] is None and view.pages[0].journal_tail_clean
    assert journal_chain(view.pages[1]) == ({"unit_temp": 1, "unit_press": 0,
                                             "reserved0": 0,
                                             "op_count": spec.RECORDS_PER_PAGE},)

    recs = records_chronological(view)
    assert len(recs) == spec.RECORDS_PER_PAGE + 5
    assert recs[0]["temp"] == -100 and recs[-1]["temp"] == 2204   # oldest-first order


def test_foreign_header_rejected() -> None:
    # The old proof-demo leftover: a small counter doubleword at the page base gives
    # version == count and page_seq == 0 -- must NOT parse as a header.
    leftover = struct.pack("<Q", 1) + b"\xff" * (spec.PAGE_SIZE - 8)
    assert parse_header(leftover) is None
    page = parse_page(leftover)
    assert page.header is None
    assert page.blank is False   # bytes present -> FOREIGN, not blank


def test_header_pad_state_reported_not_judged() -> None:
    # struct's "x" skips the 12 reserve bytes on unpack, so their state surfaces as
    # a separate pad_clean fact. The parser stays neutral: the header still parses
    # (the eval injector needs that); only the training gate rejects a dirty pad.
    recs = [make_record(1, 2200, 4500, 101325)]
    page = bytearray(make_page(make_header(), recs))
    assert parse_page(bytes(page)).pad_clean is True
    page[spec.HEADER_SIZE - 1] = 0xAB          # last pad byte no longer 0x00
    view = parse_page(bytes(page))
    assert view.pad_clean is False
    assert view.header is not None             # reported, not rejected


def test_dirty_tail_flagged_not_repaired() -> None:
    # A written slot AFTER the head: the logger never does this, so it must surface.
    recs = [make_record(1, 2200, 4500, 101325)]
    page_bytes = bytearray(make_page(make_header(), recs))
    off = spec.RECORDS_OFFSET + 3 * spec.RECORD_SIZE   # plant a record past the blank head
    page_bytes[off : off + spec.RECORD_SIZE] = make_record(9, 2300, 4400, 101000)
    page = parse_page(bytes(page_bytes))
    assert page.tail_clean is False
    assert len(page.records) == 1    # head semantics unchanged: records stop at the blank


def test_journal_gap_reported_not_judged() -> None:
    # J0 written, J1 blank, J2 written: benignly impossible (the firmware always
    # programs the next blank slot), so the fact must surface -- but the parser
    # still reports the stranded entry verbatim; the training gate judges it.
    page_bytes = bytearray(make_page(make_header(), [make_record(1, 2200, 4500, 101325)],
                                     journal=[make_journal_entry()]))
    off = spec.JOURNAL_OFFSET + 2 * spec.JOURNAL_ENTRY_SIZE
    page_bytes[off : off + spec.JOURNAL_ENTRY_SIZE] = make_journal_entry(unit_press=1,
                                                                         op_count=7)
    page = parse_page(bytes(page_bytes))
    assert page.journal_tail_clean is False
    assert page.journal[1] is None
    assert page.journal[2] == {"unit_temp": 0, "unit_press": 1, "reserved0": 0, "op_count": 7}
    assert journal_chain(page) == ({"unit_temp": 0, "unit_press": 0, "reserved0": 0,
                                    "op_count": 0},)


def test_garbage_journal_entry_reported_verbatim() -> None:
    # Out-of-range units / non-zero reserved0 are exactly what the IDS hunts; the
    # parser's job is to expose the bytes, not to sanitize them.
    entry = make_journal_entry(unit_temp=7, unit_press=250, reserved0=0xBEEF, op_count=9)
    page = parse_page(make_page(make_header(), [], journal=[entry]))
    assert page.journal[0] == {"unit_temp": 7, "unit_press": 250,
                               "reserved0": 0xBEEF, "op_count": 9}
    assert page.journal_tail_clean is True


def test_slice_nv_extracts_the_region() -> None:
    dump = bytearray(b"\x00" * DUMP_SIZE)
    dump[spec.DUMP_OFFSET : spec.DUMP_OFFSET + spec.REGION_SIZE] = b"\xab" * spec.REGION_SIZE
    assert slice_nv(bytes(dump)) == b"\xab" * spec.REGION_SIZE
    with pytest.raises(ValueError, match="expected a"):
        slice_nv(bytes(dump[:-1]))
