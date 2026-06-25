"""
manifest.jsonl reader/writer -- the dataset's source of truth.

JSON Lines (one DumpRecord per line, not one big array) so a capture run can append
atomically and a half-written last line can't corrupt earlier records.
"""

from __future__ import annotations

import json
from pathlib import Path

from offdevice.data.format import DumpRecord


def append_record(manifest_path: str | Path, record: DumpRecord) -> None:
    """Append one record as a JSON line (creating the file/dir). Append-only, so
    repeated captures never clobber existing provenance."""
    path = Path(manifest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.to_json_obj(), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_manifest(manifest_path: str | Path) -> list[DumpRecord]:
    """Parse every non-blank line into a :class:`DumpRecord`.

    Raises with the offending line number on malformed JSON or a bad record, so a
    corrupt manifest fails loudly rather than silently dropping dumps.
    """
    path = Path(manifest_path)
    records: list[DumpRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
                records.append(DumpRecord.from_json_obj(obj))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{lineno}: bad manifest record: {exc}") from exc
    return records


def write_manifest(manifest_path: str | Path, records: list[DumpRecord]) -> None:
    """(Over)write the whole manifest from a list of records.

    Use for rebuilds/tests; normal capture uses :func:`append_record`.
    """
    path = Path(manifest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record.to_json_obj(), ensure_ascii=False) + "\n")
