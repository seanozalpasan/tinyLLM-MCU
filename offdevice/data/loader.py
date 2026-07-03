"""
Size- and fingerprint-validated dump reader + manifest-driven dataset iteration.

Integrity checks: a dump must match the length its manifest records (normally
DUMP_BYTES = 256 KB), and -- wherever a manifest md5 is available -- the bytes on
disk must still hash to it. A truncated transfer would feed plausible-looking
garbage to the feature pipeline rather than erroring, and a file corrupted or
overwritten AFTER capture would train the model silently; both fail loudly here.
Returns raw bytes (which extract_features accepts); no feature work here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from offdevice.data.format import DUMP_BYTES, DumpRecord
from offdevice.data.manifest import read_manifest


def load_dump_bytes(
    path: str | Path,
    *,
    expect_bytes: int | None = DUMP_BYTES,
    expect_md5: str | None = None,
) -> bytes:
    """Read a raw ``.bin`` dump, validating its length (and md5, when given).

    ``expect_bytes=None`` skips the length check (useful for non-256 KB fixtures);
    otherwise a mismatch raises with both the expected and actual size.
    ``expect_md5`` re-verifies the manifest fingerprint against the bytes on disk,
    so post-capture corruption or an overwrite can't slip into a fit unnoticed.
    """
    raw = Path(path).read_bytes()
    if expect_bytes is not None and len(raw) != expect_bytes:
        raise ValueError(
            f"{path}: expected {expect_bytes} bytes, got {len(raw)} "
            f"(truncated/garbled transfer?)"
        )
    if expect_md5 is not None:
        actual = hashlib.md5(raw).hexdigest()
        if actual != expect_md5:
            raise ValueError(
                f"{path}: md5 {actual} != manifest {expect_md5} "
                f"(file corrupted or overwritten since capture)"
            )
    return raw


def resolve_dump_path(record: DumpRecord, root: Path) -> Path:
    """Resolve a record's ``file`` against ``root`` (manifest dir or explicit)."""
    p = Path(record.file)
    return p if p.is_absolute() else root / p


def iter_dataset(
    manifest_path: str | Path, root: str | Path | None = None
) -> Iterator[tuple[DumpRecord, bytes]]:
    """Yield ``(record, raw_bytes)`` for EVERY dump in a manifest.

    Each dump is validated against its OWN recorded ``n_bytes`` and ``md5`` (so a
    manifest can mix sizes and every record's fingerprint is honored). ``root``
    resolves relative ``file`` paths, defaulting to the manifest's directory.
    Bytes load eagerly for every record -- callers that want a subset should
    filter manifest records themselves before reading (as load_samples does), or
    one missing file outside their scope aborts the whole iteration.
    """
    manifest_path = Path(manifest_path)
    base = Path(root) if root is not None else manifest_path.parent
    for record in read_manifest(manifest_path):
        raw = load_dump_bytes(resolve_dump_path(record, base),
                              expect_bytes=record.n_bytes, expect_md5=record.md5)
        yield record, raw


def load_dataset(
    manifest_path: str | Path, root: str | Path | None = None
) -> list[tuple[DumpRecord, bytes]]:
    """Eager :func:`iter_dataset` -- materialize the whole (small) dataset."""
    return list(iter_dataset(manifest_path, root))
