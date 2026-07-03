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


def make_page(header: bytes | None, records: list[bytes]) -> bytes:
    assert len(records) <= spec.RECORDS_PER_PAGE
    body = b"".join(records)
    page = ((header if header is not None else spec.BLANK_HEADER) + body
            + spec.BLANK_RECORD * (spec.RECORDS_PER_PAGE - len(records)))
    assert len(page) == spec.PAGE_SIZE
    return page


def test_blank_region_parses_empty() -> None:
    view = parse_region(b"\xff" * spec.REGION_SIZE)
    assert view.current is None
    assert all(p.header is None and p.records == () and p.tail_clean and p.blank
               and p.pad_clean for p in view.pages)
    assert records_chronological(view) == ()


def test_round_trip_two_page_ring() -> None:
    # Page 0 = the older, full page (seq 1); page 1 = current, partially filled (seq 2).
    old_recs = [make_record(t, -100 + t, 4500 + t, 101325 + t) for t in range(spec.RECORDS_PER_PAGE)]
    new_recs = [make_record(t, 2200 + t, 4400, 101300) for t in range(5)]
    nv = (make_page(make_header(page_seq=1, op_count=0), old_recs)
          + make_page(make_header(page_seq=2, boot_count=2, op_count=spec.RECORDS_PER_PAGE),
                      new_recs))

    view = parse_region(nv)
    assert view.current == 1
    assert view.pages[0].header is not None and view.pages[0].header["page_seq"] == 1
    assert view.pages[1].header is not None and view.pages[1].header["op_count"] == 124
    assert len(view.pages[0].records) == spec.RECORDS_PER_PAGE
    assert len(view.pages[1].records) == 5
    assert view.pages[1].records[3] == {"ts": 3, "temp": 2203, "hum": 4400, "press": 101300}

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
    off = spec.HEADER_SIZE + 3 * spec.RECORD_SIZE   # plant a record past the blank head
    page_bytes[off : off + spec.RECORD_SIZE] = make_record(9, 2300, 4400, 101000)
    page = parse_page(bytes(page_bytes))
    assert page.tail_clean is False
    assert len(page.records) == 1    # head semantics unchanged: records stop at the blank


def test_slice_nv_extracts_the_region() -> None:
    dump = bytearray(b"\x00" * DUMP_SIZE)
    dump[spec.DUMP_OFFSET : spec.DUMP_OFFSET + spec.REGION_SIZE] = b"\xab" * spec.REGION_SIZE
    assert slice_nv(bytes(dump)) == b"\xab" * spec.REGION_SIZE
    with pytest.raises(ValueError, match="expected a"):
        slice_nv(bytes(dump[:-1]))
