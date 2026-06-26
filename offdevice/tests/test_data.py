"""
Unit tests for offdevice/data: record/JSON round-trip, manifest I/O, the
size-validating loader, and an end-to-end pass over the refs sample dumps.

Run from the repo root (so `import offdevice...` resolves):
    pytest offdevice\\tests\\test_data.py -v
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from offdevice.data.format import (
    DUMP_BYTES,
    LABEL_TO_INT,
    REFS_DUMP_BYTES,
    DumpRecord,
    build_filename,
)
from offdevice.data.loader import iter_dataset, load_dump_bytes
from offdevice.data.manifest import append_record, read_manifest, write_manifest
from offdevice.data.validate_refs import EXPECTED_REFS_DUMPS, find_refs_dumps


# A real verified chip md5 of an NS-flash image -- a genuine 32-hex digest, so the
# fixture mirrors an actual capture rather than a made-up string.
_SAMPLE_MD5 = "70660e116f908c1cfe560eb9f1bfa350"


def _record(file: str = "benign__tbA__t1__run001__19700101T0000.bin") -> DumpRecord:
    return DumpRecord(
        file=file,
        label="benign",
        testbed="tbA",
        capture_point="loop-quiesced",
        mem_range="0x08040000-0x0807FFFF",
        md5=_SAMPLE_MD5,
        ts="2026-06-20T15:30:00",
        conditions={"temp_c": 23.4},
    )


def test_record_json_round_trip() -> None:
    rec = _record()
    again = DumpRecord.from_json_obj(rec.to_json_obj())
    assert again == rec
    assert again.label_int == LABEL_TO_INT["benign"] == 1


def test_record_rejects_bad_label() -> None:
    with pytest.raises(ValueError, match="label must be one of"):
        _record_kwargs = dict(
            file="x.bin", label="suspicious", testbed="tbA",
            capture_point="x", mem_range="x", md5=_SAMPLE_MD5, ts="x",
        )
        DumpRecord(**_record_kwargs)  # type: ignore[arg-type]


# Each case mutates the known-good digest one way it can go wrong -- wrong case, wrong
# length, a prefix, a non-hex char -- so the defect is visible in the expression itself.
@pytest.mark.parametrize("bad_md5", [
    "",
    _SAMPLE_MD5.upper(),
    _SAMPLE_MD5[:-1],
    _SAMPLE_MD5 + "0",
    "0x" + _SAMPLE_MD5,
    _SAMPLE_MD5[:-1] + "z",
])
def test_record_rejects_bad_md5(bad_md5: str) -> None:
    with pytest.raises(ValueError, match="md5 must be lowercase 32-hex"):
        DumpRecord(
            file="x.bin", label="benign", testbed="tbA",
            capture_point="x", mem_range="x", md5=bad_md5, ts="x",
        )


def test_from_json_obj_requires_keys() -> None:
    # md5 is required, not defaulted -- a record missing it must be rejected, not filled.
    with pytest.raises(ValueError, match=r"missing required keys.*md5"):
        DumpRecord.from_json_obj({"file": "x.bin", "label": "benign"})


def test_to_json_obj_key_order() -> None:
    # The manifest is long-lived, append-only JSON Lines, so the on-disk key order is a
    # contract -- lock it here so a refactor can't silently reshuffle the schema.
    assert list(_record().to_json_obj().keys()) == [
        "file", "label", "testbed", "conditions", "capture_point",
        "mem_range", "sr", "md5", "n_bytes", "ts",
    ]


def test_build_filename() -> None:
    name = build_filename("benign", "tbA", "temp23p4", 12, "20260620T1530")
    assert name == "benign__tbA__temp23p4__run012__20260620T1530.bin"
    with pytest.raises(ValueError):
        build_filename("nope", "tbA", "t", 1, "ts")


def test_manifest_append_and_read(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    recs = [_record("a.bin"), _record("b.bin")]
    for r in recs:
        append_record(manifest, r)
    assert read_manifest(manifest) == recs


def test_manifest_skips_blank_lines(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest, [_record("a.bin")])
    with manifest.open("a", encoding="utf-8") as fh:
        fh.write("\n   \n")
    assert len(read_manifest(manifest)) == 1


def test_manifest_bad_line_reports_lineno(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r":1: bad manifest record"):
        read_manifest(manifest)


def test_loader_validates_length(tmp_path: Path) -> None:
    short = tmp_path / "short.bin"
    short.write_bytes(b"\x00" * 1024)
    # default expectation is the 256 KB NS-flash range (262144 bytes)
    assert DUMP_BYTES == 262_144
    with pytest.raises(ValueError, match="expected 262144 bytes, got 1024"):
        load_dump_bytes(short)
    # explicit expect_bytes=None disables the check
    assert len(load_dump_bytes(short, expect_bytes=None)) == 1024


# --- refs integration (skipped if the read-only refs checkout is absent) -----

@pytest.mark.skipif(not find_refs_dumps(), reason="refs sample dumps not present")
def test_refs_dumps_found_complete() -> None:
    # the discovery itself is under test: all 7 must be found, none silently dropped
    assert len(find_refs_dumps()) == EXPECTED_REFS_DUMPS


@pytest.mark.skipif(not find_refs_dumps(), reason="refs sample dumps not present")
def test_refs_dumps_are_256k_and_round_trip(tmp_path: Path) -> None:
    dumps = find_refs_dumps()
    assert len(dumps) == EXPECTED_REFS_DUMPS  # fail loudly if a dump goes missing
    manifest = tmp_path / "manifest.jsonl"
    for dump in dumps:
        # refs are 256 KB flash dumps -- same source + size as our captures
        append_record(manifest, DumpRecord(
            file=str(dump), label="benign", testbed="ref",
            capture_point="ref", mem_range="ref",
            md5=hashlib.md5(dump.read_bytes()).hexdigest(),
            ts="1970-01-01T00:00:00", n_bytes=REFS_DUMP_BYTES,
        ))
    loaded = list(iter_dataset(manifest))
    assert len(loaded) == len(dumps)
    for _record_, raw in loaded:
        assert len(raw) == REFS_DUMP_BYTES
