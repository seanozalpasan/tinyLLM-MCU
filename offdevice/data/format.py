"""
Dataset dump format -- the single source of truth for what a captured dump is and
how its metadata is recorded.

The dump is a raw 256 KB .bin. Everything else -- label, test-bed, conditions,
memory range, whole-dump MD5 -- lives in a manifest.jsonl record (and is mirrored
in the filename for humans). The label is NEVER inferred from the bytes: it comes
from the capture process and is recorded here.

Schema + validation + JSON round-trip + filename convention live here; the manifest
reader/writer is in manifest.py, the size-validating reader in loader.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- Dump size ---------------------------------------------------------------
# Captures snapshot the NonSecure internal flash image, 0x08040000-0x0807FFFF =
# 256 KB (static program image). Same source + size as the refs/mars-original
# sample dumps, so there is ONE constant. A short read => a truncated transfer.
NS_FLASH_ORIGIN = 0x0804_0000
NS_FLASH_END = 0x0807_FFFF                 # inclusive
NS_FLASH_RANGE = "0x08040000-0x0807FFFF"   # canonical DumpRecord.mem_range string
DUMP_BYTES = 256 * 1024                    # 262_144

# refs/mars-original sample dumps (plumbing fixtures only, never training data)
# are the same 256 KB flash dumps as ours.
REFS_DUMP_BYTES = DUMP_BYTES

# Each manifest record also stores its OWN n_bytes, and the loader validates
# against that -- so a dataset can mix sizes and a change here never silently
# invalidates older captures.

# Label vocabulary. Int codes follow MARS (benign=1, anomalous=0); the LLM emits
# the WORD, the int is only for off-device metrics/bookkeeping.
LABELS: tuple[str, str] = ("benign", "anomalous")
LABEL_TO_INT: dict[str, int] = {"benign": 1, "anomalous": 0}

# Filename convention, e.g. benign__tbA__temp23p4__run012__20260620T1530.bin
# (the manifest is authoritative; the filename is a human-readable mirror).
FILENAME_SEP = "__"
FILENAME_SUFFIX = ".bin"

# Whole-dump MD5 fingerprint: lowercase 32-hex, exactly as both
# hashlib.md5(payload).hexdigest() and the on-chip hardware HASH peripheral emit it.
# A record's md5 is rejected unless it matches this, so a malformed digest (uppercase,
# "0x"-prefixed, wrong length, raw bytes) can't slip into the manifest.
MD5_HEX_RE = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class DumpRecord:
    """One manifest.jsonl line -- the provenance of a single dump.

    md5 is the whole-dump fingerprint (== the on-chip hardware-HASH digest and the
    host's hashlib re-check); n_bytes is stored per-record so the loader catches a
    truncated capture.
    """

    file: str               # dump filename or path, relative to the manifest dir
    label: str              # "benign" | "anomalous" -- from the capture, not the bytes
    testbed: str            # "tbA" | "tbB" (or "ref" for plumbing fixtures)
    capture_point: str      # where in the workload loop the snapshot was taken
    mem_range: str          # e.g. "0x08040000-0x0807FFFF" (the NS-flash span)
    md5: str                # whole-dump MD5, lowercase 32-hex (integrity + fingerprint)
    ts: str                 # ISO-8601 capture timestamp, e.g. "2026-06-20T15:30:00"
    sr: int = 22_050        # feature sample rate this dump is destined for (contract tie)
    n_bytes: int = DUMP_BYTES
    conditions: dict[str, object] = field(default_factory=dict)  # free-form capture knobs

    def __post_init__(self) -> None:
        if self.label not in LABELS:
            raise ValueError(f"label must be one of {LABELS}, got {self.label!r}")
        if MD5_HEX_RE.fullmatch(self.md5) is None:
            raise ValueError(f"md5 must be lowercase 32-hex, got {self.md5!r}")
        if self.n_bytes <= 0:
            raise ValueError(f"n_bytes must be positive, got {self.n_bytes}")

    @property
    def label_int(self) -> int:
        return LABEL_TO_INT[self.label]

    def to_json_obj(self) -> dict[str, object]:
        """JSON-serializable dict (one manifest line), stable key order."""
        return {
            "file": self.file,
            "label": self.label,
            "testbed": self.testbed,
            "conditions": self.conditions,
            "capture_point": self.capture_point,
            "mem_range": self.mem_range,
            "sr": self.sr,
            "md5": self.md5,
            "n_bytes": self.n_bytes,
            "ts": self.ts,
        }

    @classmethod
    def from_json_obj(cls, obj: dict[str, object]) -> DumpRecord:
        """Inverse of to_json_obj -- tolerant of missing OPTIONAL fields (sr, n_bytes,
        conditions). Every required key -- including md5 -- must be present."""
        required = ("file", "label", "testbed", "capture_point",
                    "mem_range", "md5", "ts")
        missing = [k for k in required if k not in obj]
        if missing:
            raise ValueError(f"manifest record missing required keys: {missing}")
        return cls(
            file=str(obj["file"]),
            label=str(obj["label"]),
            testbed=str(obj["testbed"]),
            capture_point=str(obj["capture_point"]),
            mem_range=str(obj["mem_range"]),
            md5=str(obj["md5"]),
            ts=str(obj["ts"]),
            sr=int(obj.get("sr", 22_050)),  # type: ignore[arg-type]
            n_bytes=int(obj.get("n_bytes", DUMP_BYTES)),  # type: ignore[arg-type]
            conditions=dict(obj.get("conditions", {})),  # type: ignore[arg-type]
        )


def build_filename(
    label: str, testbed: str, conditions_tag: str, run: int, ts_compact: str
) -> str:
    """Compose a dump filename, e.g. benign__tbA__temp23p4__run012__20260620T1530.bin.

    conditions_tag is a filesystem-safe conditions summary (e.g. "temp23p4");
    ts_compact is a compact timestamp (e.g. "20260620T1530").
    """
    if label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}, got {label!r}")
    parts = (label, testbed, conditions_tag, f"run{run:03d}", ts_compact)
    return FILENAME_SEP.join(parts) + FILENAME_SUFFIX
