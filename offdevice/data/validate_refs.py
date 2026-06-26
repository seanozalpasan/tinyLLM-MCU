"""
Smoke test: run the format + manifest + loader plumbing over the refs sample dumps,
proving the round-trip works on real 256 KB .bin files before our own dumps exist.

IMPORTANT: refs dumps are PLUMBING FIXTURES ONLY -- never training data. This labels
them "benign" purely so DumpRecord validates, and writes the manifest to a throwaway
temp dir. Do not point training at this manifest.

Run (from repo root, .venv active):
    python -m offdevice.data.validate_refs
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from offdevice.data.format import REFS_DUMP_BYTES, DumpRecord
from offdevice.data.loader import iter_dataset
from offdevice.data.manifest import append_record, read_manifest

# repo root = .../tinyLLM-MCU  (this file is offdevice/data/validate_refs.py)
REPO_ROOT = Path(__file__).resolve().parents[2]
REFS_DIR = REPO_ROOT / "refs" / "mars-original" / "Classification-Server-Scripts"

# refs ships exactly 7 sample dumps; asserting the count makes a silently-missing
# file fail loudly instead of passing on a smaller set.
EXPECTED_REFS_DUMPS = 7


def find_refs_dumps() -> list[Path]:
    """Every .bin under refs/ (recursive, sorted). Recursive because the dumps sit
    at three different depths -- a recursive glob can't miss one the way a
    hand-listed path can."""
    return sorted(REFS_DIR.glob("**/*.bin"))


def main() -> int:
    dumps = find_refs_dumps()
    if not dumps:
        print(f"FAIL: no refs dumps found under {REFS_DIR}")
        return 1
    note = "" if len(dumps) == EXPECTED_REFS_DUMPS else f"  <-- EXPECTED {EXPECTED_REFS_DUMPS}!"
    print(f"found {len(dumps)} refs sample dump(s) under {REFS_DIR}{note}\n")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "manifest.jsonl"

        # 1. write one append-only manifest record per dump
        for dump in dumps:
            append_record(manifest, DumpRecord(
                file=str(dump),  # absolute -> loader uses it as-is
                label="benign",
                testbed="ref",
                capture_point="n/a-refs-fixture",
                mem_range="n/a-refs-fixture",
                md5=hashlib.md5(dump.read_bytes()).hexdigest(),  # real digest of the fixture bytes
                ts="1970-01-01T00:00:00",
                n_bytes=REFS_DUMP_BYTES,  # 256 KB flash dumps -- same source + size as our captures
                conditions={"note": "refs plumbing fixture -- NOT training data"},
            ))

        # 2. round-trip the manifest (write -> read -> same count)
        records = read_manifest(manifest)
        assert len(records) == len(dumps), "manifest round-trip lost records"
        print(f"manifest round-trip ok: {len(records)} records\n")

        # 3. load every dump through the real loader (validates length per-record)
        ok = True
        for record, raw in iter_dataset(manifest):
            size_ok = len(raw) == REFS_DUMP_BYTES
            ok &= size_ok
            flag = "ok" if size_ok else f"BAD ({len(raw)} != {REFS_DUMP_BYTES})"
            print(f"  {Path(record.file).name:24} {len(raw):>7} bytes  label={record.label:9} {flag}")

    count_ok = len(dumps) == EXPECTED_REFS_DUMPS
    print()
    if ok and count_ok:
        print(f"PASS -- format + manifest + loader round-trip all {len(dumps)} refs dumps.")
        return 0
    if not count_ok:
        print(f"FAIL -- found {len(dumps)} dumps, expected {EXPECTED_REFS_DUMPS} "
              f"(a sample dump went missing).")
    if not ok:
        print(f"FAIL -- at least one dump was not {REFS_DUMP_BYTES} bytes.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
