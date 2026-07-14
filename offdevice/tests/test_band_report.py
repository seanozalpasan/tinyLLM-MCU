"""
Tests for offdevice/data/band_report.py -- classification and the missing-count
arithmetic, on synthetic 256 KB captures built from the golden fixture (no real
dumps needed).
"""

from __future__ import annotations

import struct
from pathlib import Path

from offdevice.data.band_report import BAND_HI, BAND_LO, main, scan_capture
from offdevice.nv import spec
from offdevice.nv.parse import DUMP_SIZE
from offdevice.tests import fixtures


def _dump_with_region(region: bytes) -> bytes:
    """Wrap a 4 KB NV region into a full-size dump image (static part zeroed)."""
    return b"\x00" * spec.DUMP_OFFSET + region


def _region_with_page1_records(n: int) -> bytes:
    """The fixture region grown to n page-1 records (ring total = 122 + n)."""
    region = bytearray(fixtures.synthetic_nv_region())
    base = spec.PAGE_SIZE + spec.RECORDS_OFFSET
    for i in range(fixtures.PAGE1_RECORDS, n):
        rec = struct.pack(spec.RECORD_FMT,
                          fixtures.TS_FIRST + fixtures.TS_STEP * (spec.RECORDS_PER_PAGE + i),
                          fixtures.TEMP_MID, fixtures.HUM_MID, fixtures.PRESS_MID)
        region[base + i * spec.RECORD_SIZE: base + (i + 1) * spec.RECORD_SIZE] = rec
    return bytes(region)


def test_scan_totals_full_dump_and_bare_region(tmp_path: Path):
    dump = tmp_path / "dump.bin"
    dump.write_bytes(_dump_with_region(fixtures.synthetic_nv_region()))
    bare = tmp_path / "bare.bin"
    bare.write_bytes(fixtures.synthetic_nv_region())
    expected = spec.RECORDS_PER_PAGE + fixtures.PAGE1_RECORDS
    assert scan_capture(dump) == (expected, "")
    assert scan_capture(bare) == (expected, "")


def test_scan_rejects_wrong_size(tmp_path: Path):
    stub = tmp_path / "short.bin"
    stub.write_bytes(b"\x00" * 1024)
    total, note = scan_capture(stub)
    assert total == -1
    assert "skipped" in note


def test_scan_flags_foreign_page_and_skips_its_slots(tmp_path: Path):
    region = b"\xab" * spec.PAGE_SIZE + fixtures.synthetic_nv_region()[spec.PAGE_SIZE:]
    path = tmp_path / "foreign.bin"
    path.write_bytes(_dump_with_region(region))
    total, note = scan_capture(path)
    assert "FOREIGN" in note
    # The foreign page's garbage slots must not count -- only the valid page's.
    assert total == fixtures.PAGE1_RECORDS


def test_report_counts_band_and_missing(tmp_path: Path, capsys):
    in_band_total = spec.RECORDS_PER_PAGE + 120   # 242: inside 238..244
    assert BAND_LO <= in_band_total <= BAND_HI
    (tmp_path / "band.bin").write_bytes(_dump_with_region(_region_with_page1_records(120)))
    (tmp_path / "mid.bin").write_bytes(_dump_with_region(fixtures.synthetic_nv_region()))
    (tmp_path / "empty.bin").write_bytes(
        _dump_with_region(bytes([spec.ERASED_BYTE]) * spec.REGION_SIZE))

    assert main([str(tmp_path), "--target", "2"]) == 1
    out = capsys.readouterr().out
    assert f"1 in the {BAND_LO}..{BAND_HI} band" in out
    assert "MISSING 1" in out

    assert main([str(tmp_path), "--target", "1"]) == 0
    assert "met" in capsys.readouterr().out


def test_report_empty_folder(tmp_path: Path, capsys):
    assert main([str(tmp_path)]) == 1
    assert "no .bin captures" in capsys.readouterr().out
