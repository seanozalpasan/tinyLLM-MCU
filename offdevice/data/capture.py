"""
Capture one NS-flash dump from the board over serial -> a .bin + a manifest record.

Wire protocol (must match the firmware's flash_dump.{c,h}): the host sends 'D', and the
board streams, on USART1 @ 921600 8N1, ASCII status lines interleaved with one binary frame

    [sentinel "MARSDMP1" 8B][len u32 LE][payload `len` B][md5 16B]

then a trailing ASCII "DUMP done ..." line. We scan the byte stream for the sentinel, so
the leading ASCII banner/echo self-skips; then we read a FIXED len+16 bytes. Syncing on the
FIRST sentinel makes a coincidental "MARSDMP1" inside the payload harmless. The whole-dump
md5 is recomputed here and must equal the 16 bytes the board sent -- any length or md5
mismatch aborts WITHOUT writing, so corrupted bytes never enter the dataset.

On success: writes <captures>/<benign__tbA__variant__runNNN__ts>.bin and appends a
DumpRecord to <captures>/manifest.jsonl. The record's `file` is the bare filename, so the
captures dir is a self-contained, relocatable dataset.

Run (from repo root, .venv active; board flashed with a DUMP_NSFLASH=1 build, on COM3):
    python -m offdevice.data.capture <variant-tag>
e.g.
    python -m offdevice.data.capture tbA-benign-rawpass-v1
"""

from __future__ import annotations

import argparse
import hashlib
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

import serial  # pyserial

from offdevice.data.format import (
    DUMP_BYTES,
    NS_FLASH_RANGE,
    DumpRecord,
    build_filename,
)
from offdevice.data.manifest import append_record, read_manifest

# Firmware contract (flash_dump.{c,h}): trigger byte, frame sentinel, field widths.
TRIGGER = b"D"
SENTINEL = b"MARSDMP1"
LEN_FIELD_BYTES = 4          # u32 LE payload length
MD5_FIELD_BYTES = 16         # raw md5 digest

# 262144 B at 921600 8N1 (~10 bits/byte) is ~2.9 s on the wire. The per-read timeout just
# bounds how often we re-check the overall deadline; the deadline bounds the whole transfer.
SERIAL_READ_TIMEOUT_S = 2.0
TRANSFER_DEADLINE_S = 30.0
# The ST-Link VCP can pulse DTR/RTS and reset the target when the port opens; give it a
# moment to reach the dump receive-loop before we send 'D'. Harmless if it doesn't reset.
PORT_SETTLE_S = 0.5

# Default capture location: a self-contained, gitignored dir holding the manifest + .bin.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAPTURES_DIR = REPO_ROOT / "offdevice" / "data" / "captures"
MANIFEST_NAME = "manifest.jsonl"

# The variant tag becomes a "__"-delimited filename field, so keep it to characters that
# can't break that split or a path component.
VARIANT_RE = re.compile(r"[A-Za-z0-9.+-]+")


class ByteReader(Protocol):
    """The minimal interface the frame parser needs -- satisfied by serial.Serial and,
    for tests, io.BytesIO."""

    def read(self, n: int, /) -> bytes: ...


class CaptureError(RuntimeError):
    """A dump that failed to arrive intact -- nothing is written when this is raised."""


def _read_exact(reader: ByteReader, n: int, deadline: float) -> bytes:
    """Read exactly n bytes, or raise CaptureError once the transfer deadline passes."""
    buf = bytearray()
    while len(buf) < n:
        if time.monotonic() > deadline:
            raise CaptureError(f"timed out after {len(buf)}/{n} bytes -- stalled transfer")
        buf += reader.read(n - len(buf))
    return bytes(buf)


def _sync_to_sentinel(reader: ByteReader, deadline: float) -> None:
    """Discard bytes until the sentinel passes, skipping the ASCII banner/echo.

    Reads one byte at a time over a sliding window -- the preamble is short, and the bulk
    payload is read in one shot by _read_exact once we are aligned.
    """
    window = bytearray()
    while True:
        if time.monotonic() > deadline:
            raise CaptureError("sentinel 'MARSDMP1' never seen -- wrong port, not a DUMP_NSFLASH build, or the board reset on open mid-boot (re-run).")
        b = reader.read(1)
        if not b:
            continue
        window += b
        if len(window) > len(SENTINEL):
            del window[0]
        if window == SENTINEL:
            return


def parse_dump_stream(reader: ByteReader, deadline: float) -> tuple[bytes, str]:
    """Read one verified frame from an already-triggered stream; return (payload, md5_hex).

    Raises CaptureError on any framing, length, or md5 mismatch -- the caller writes
    nothing in that case.
    """
    _sync_to_sentinel(reader, deadline)
    n = int.from_bytes(_read_exact(reader, LEN_FIELD_BYTES, deadline), "little")
    if n != DUMP_BYTES:
        raise CaptureError(f"frame len {n} != expected {DUMP_BYTES}")
    payload = _read_exact(reader, n, deadline)
    received_md5 = _read_exact(reader, MD5_FIELD_BYTES, deadline).hex()

    computed_md5 = hashlib.md5(payload).hexdigest()
    if computed_md5 != received_md5:
        raise CaptureError(
            f"md5 mismatch: board sent {received_md5}, payload hashes to {computed_md5}"
        )
    return payload, computed_md5


def _next_run(manifest_path: Path, variant: str) -> int:
    """0-based count of existing records with this variant -- keeps filenames unique."""
    if not manifest_path.exists():
        return 0
    return sum(1 for rec in read_manifest(manifest_path)
               if rec.conditions.get("variant") == variant)


def capture(port_name: str, variant: str, label: str, testbed: str,
            capture_point: str, captures_dir: Path, baud: int) -> Path:
    """Drive one capture end to end; return the written .bin path."""
    if VARIANT_RE.fullmatch(variant) is None:
        raise CaptureError(f"variant tag must be [A-Za-z0-9.+-], got {variant!r}")

    with serial.Serial(port_name, baud, timeout=SERIAL_READ_TIMEOUT_S) as port:
        print(f"[capture] {port_name} @ {baud} 8N1 -- sending '{TRIGGER.decode()}' ...")
        time.sleep(PORT_SETTLE_S)
        port.reset_input_buffer()      # drop any stale banner bytes before triggering
        port.write(TRIGGER)
        payload, md5_hex = parse_dump_stream(port, time.monotonic() + TRANSFER_DEADLINE_S)
    print(f"[capture] received {len(payload)} bytes, md5={md5_hex}")

    manifest_path = captures_dir / MANIFEST_NAME
    now = datetime.now()
    filename = build_filename(label, testbed, variant,
                              _next_run(manifest_path, variant),
                              now.strftime("%Y%m%dT%H%M%S"))

    # Write the .bin BEFORE the manifest line, so the manifest never points at a missing
    # file if the process dies between the two.
    captures_dir.mkdir(parents=True, exist_ok=True)
    bin_path = captures_dir / filename
    bin_path.write_bytes(payload)
    append_record(manifest_path, DumpRecord(
        file=filename,                 # bare name -> resolved against the manifest dir
        label=label,
        testbed=testbed,
        capture_point=capture_point,
        mem_range=NS_FLASH_RANGE,
        md5=md5_hex,
        ts=now.isoformat(timespec="seconds"),
        conditions={"variant": variant},
    ))
    print(f"[capture] wrote {bin_path}")
    print(f"[capture] appended record to {manifest_path}")
    return bin_path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Capture one NS-flash dump from the board to a .bin + manifest record.")
    ap.add_argument("variant", help="human-readable build tag, e.g. tbA-benign-rawpass-v1")
    ap.add_argument("--port", default="COM3", help="serial port (ST-Link VCP); default COM3")
    ap.add_argument("--baud", type=int, default=921600, help="must match the firmware (921600)")
    ap.add_argument("--label", default="benign", choices=("benign", "anomalous"))
    ap.add_argument("--testbed", default="tbA")
    ap.add_argument("--capture-point", default="ns-flash-static",
                    help="dump builds don't run the NS workload, so the image is static")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_CAPTURES_DIR,
                    help="dir holding the manifest + .bin files")
    args = ap.parse_args()

    try:
        capture(args.port, args.variant, args.label, args.testbed,
                args.capture_point, args.out_dir, args.baud)
    except (CaptureError, serial.SerialException) as exc:
        print(f"[capture] ABORTED -- {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
