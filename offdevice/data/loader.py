"""
Size-validated dump reader + manifest-driven dataset iteration.

Integrity check: a dump must match the length its manifest records (normally
DUMP_BYTES = 256 KB). A truncated transfer would feed plausible-looking garbage to
the feature pipeline rather than erroring, so we validate length up front and fail
loudly. Returns raw bytes (which extract_features accepts); no feature work here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from offdevice.data.format import DUMP_BYTES, DumpRecord
from offdevice.data.manifest import read_manifest


def load_dump_bytes(
    path: str | Path, *, expect_bytes: int | None = DUMP_BYTES
) -> bytes:
    """Read a raw ``.bin`` dump, validating its length.

    ``expect_bytes=None`` skips the length check (useful for non-256 KB fixtures);
    otherwise a mismatch raises with both the expected and actual size.
    """
    raw = Path(path).read_bytes()
    if expect_bytes is not None and len(raw) != expect_bytes:
        raise ValueError(
            f"{path}: expected {expect_bytes} bytes, got {len(raw)} "
            f"(truncated/garbled transfer?)"
        )
    return raw


def _resolve(record: DumpRecord, root: Path) -> Path:
    """Resolve a record's ``file`` against ``root`` (manifest dir or explicit)."""
    p = Path(record.file)
    return p if p.is_absolute() else root / p


def iter_dataset(
    manifest_path: str | Path, root: str | Path | None = None
) -> Iterator[tuple[DumpRecord, bytes]]:
    """Yield ``(record, raw_bytes)`` for every dump in a manifest.

    Each dump is validated against its OWN recorded ``n_bytes`` (so a manifest can
    mix sizes), not the global default. ``root`` resolves relative ``file`` paths,
    defaulting to the manifest's directory.
    """
    manifest_path = Path(manifest_path)
    base = Path(root) if root is not None else manifest_path.parent
    for record in read_manifest(manifest_path):
        raw = load_dump_bytes(_resolve(record, base), expect_bytes=record.n_bytes)
        yield record, raw


def load_dataset(
    manifest_path: str | Path, root: str | Path | None = None
) -> list[tuple[DumpRecord, bytes]]:
    """Eager :func:`iter_dataset` -- materialize the whole (small) dataset."""
    return list(iter_dataset(manifest_path, root))
