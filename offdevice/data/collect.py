"""
Unattended benign-dataset collection: reset the board, capture, sleep, repeat.

Builds the benign NV dataset with zero hands-on time: each cycle hardware-resets
the board through the ST-LINK's debug interface (STM32_Programmer_CLI), then
triggers the firmware's boot-time capture window (flash_dump.c, DUMP_NSFLASH=2)
and saves the verified dump via the same .bin + manifest path as capture.py.
The board must run a DUMP_NSFLASH=2 build; captures land before the NS workload
starts each boot, so every snapshot is a frozen, consistent ring.

The debug interface and the serial port ride the same ST-LINK USB cable, so the
port stays open across target resets and no cable juggling is needed. Every
capture necessarily reboots the board -- the resulting timestamp restart in the
ring is benign by spec and belongs in the training data (real devices reboot).

Run (repo root; needs only pyserial + STM32CubeProgrammer installed):
    python -m offdevice.data.collect nv45s-lab-w1 --interval 2.0
    python -m offdevice.data.collect nv45s-lab-w1 --fresh --interval 2.0
    python -m offdevice.data.collect nv45s-smoke --interval 0.05 --count 3

--fresh erases the two NV pages first (a virgin ring; the following captures walk
the fill states as it refills). Stop an open-ended run with Ctrl+C; the manifest
is appended per capture, so a killed run loses nothing already saved.
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import serial  # pyserial

from offdevice.data.capture import (
    DEFAULT_CAPTURES_DIR,
    SENTINEL,
    TRIGGER,
    CaptureError,
    read_frame,
    save_capture,
)

# ---- board / CLI contract --------------------------------------------------------

# STM32_Programmer_CLI ships with STM32CubeProgrammer; this is its default install
# home. Override with --cli if it lives elsewhere.
DEFAULT_CLI = Path(r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer"
                   r"\bin\STM32_Programmer_CLI.exe")

# HOTPLUG attaches without disturbing the target; -rst then pulses NRST and the
# CLI disconnects, leaving the board to boot. (-hardRst is the fallback spelling
# on some CubeProgrammer versions if -rst ever reports "invalid command".)
RESET_ARGS = ("-c", "port=SWD", "mode=HOTPLUG", "-rst")

# The NV ring = the last two 2 KB flash pages, 0x0807F000..0x0807FFFF = global
# page indices 254/255 ((0x0807F000 - 0x08000000) / 0x800 = 254). Erasing ONLY
# these leaves all firmware intact; the next boot opens a virgin ring.
ERASE_ARGS = ("-c", "port=SWD", "mode=HOTPLUG", "-e", "254", "255")

TRIGGER_PERIOD_S = 0.25       # re-send 'D' this often until the sentinel appears
READ_TIMEOUT_S = 0.05         # short poll so re-triggering stays responsive
SYNC_DEADLINE_S = 20.0        # reset -> boot prints -> window -> sentinel
TRANSFER_DEADLINE_S = 30.0    # 256 KB @ 921600 is ~2.9 s; generous margin

# ---- unattended-run policy -------------------------------------------------------

RETRY_DELAY_S = 60.0          # pause after a failed cycle before retrying
MAX_CONSECUTIVE_FAILURES = 10  # then abort: something is wrong, not transient
LOG_NAME = "collect.log"

# Windows power request: a weekend run must survive the host's sleep policy.
# (Keeps the system awake, not the display.)
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class CollectError(RuntimeError):
    """A cycle that failed before a verified payload existed -- nothing was written."""


def _log(log_path: Path, msg: str) -> None:
    """Print + append one timestamped line -- the audit trail of an unattended run."""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _run_cli(cli: Path, args: tuple[str, ...]) -> None:
    """Run one STM32_Programmer_CLI command; raise CollectError with its tail on failure."""
    proc = subprocess.run([str(cli), *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
        raise CollectError(f"{cli.name} {' '.join(args)} failed (rc={proc.returncode}): "
                           + " | ".join(tail))


def _sync_with_retrigger(port: serial.Serial, deadline: float) -> None:
    """Send 'D' periodically while scanning for the sentinel.

    A single 'D' can be lost around the reset (the target's UART isn't clocked
    while in reset), so we re-send until the frame starts. The scan window
    persists across re-sends -- a sentinel straddling two reads still matches.
    Extra 'D's are harmless: the boot window serves one dump then drops them.
    """
    window = bytearray()
    next_send = 0.0
    while True:
        now = time.monotonic()
        if now > deadline:
            raise CaptureError(
                "sentinel 'MARSDMP1' never seen -- board not on this port, not a "
                "DUMP_NSFLASH=2 build, or the reset command didn't reboot it")
        if now >= next_send:
            port.write(TRIGGER)
            next_send = now + TRIGGER_PERIOD_S
        b = port.read(1)
        if not b:
            continue
        window += b
        if len(window) > len(SENTINEL):
            del window[0]
        if window == SENTINEL:
            return


def collect_once(cli: Path, port_name: str, baud: int, variant: str,
                 captures_dir: Path) -> Path:
    """One cycle: open port -> reset board -> trigger + receive -> save; return the .bin."""
    with serial.Serial(port_name, baud, timeout=READ_TIMEOUT_S) as port:
        port.reset_input_buffer()          # drop the previous boot's console bytes
        _run_cli(cli, RESET_ARGS)          # port is ST-LINK-side; it survives the reset
        _sync_with_retrigger(port, time.monotonic() + SYNC_DEADLINE_S)
        payload, md5_hex = read_frame(port, time.monotonic() + TRANSFER_DEADLINE_S)
    return save_capture(payload, md5_hex, variant, label="benign", testbed="tbA",
                        capture_point="boot-window", captures_dir=captures_dir)


def _keep_awake() -> None:
    """Ask Windows not to sleep while we run (no-op elsewhere)."""
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def _release_awake() -> None:
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def run(cli: Path, port_name: str, baud: int, variant: str, captures_dir: Path,
        interval_h: float, count: int | None, fresh: bool) -> int:
    """The collection loop; returns a process exit code."""
    captures_dir.mkdir(parents=True, exist_ok=True)
    log_path = captures_dir / LOG_NAME

    if not cli.exists():
        print(f"[collect] STM32_Programmer_CLI not found at {cli} -- pass --cli", flush=True)
        return 1

    _log(log_path, f"collect start: variant={variant} interval={interval_h}h "
                   f"count={'unbounded' if count is None else count} fresh={fresh}")
    if fresh:
        # Erase THEN reset (via the first cycle) -- the reboot re-inits the logger
        # on the blank pages, so the ring restarts cleanly from a virgin state.
        _run_cli(cli, ERASE_ARGS)
        _log(log_path, "NV pages 254/255 (0x0807F000..0x0807FFFF) erased -- virgin ring")

    done = 0
    failures = 0
    _keep_awake()
    try:
        while count is None or done < count:
            try:
                bin_path = collect_once(cli, port_name, baud, variant, captures_dir)
                done += 1
                failures = 0
                _log(log_path, f"capture {done} ok: {bin_path.name}")
            except (CaptureError, CollectError, serial.SerialException,
                    subprocess.TimeoutExpired) as exc:
                failures += 1
                _log(log_path, f"cycle FAILED ({failures}/{MAX_CONSECUTIVE_FAILURES}): {exc}")
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    _log(log_path, "aborting: consecutive-failure limit reached")
                    return 1
                time.sleep(RETRY_DELAY_S)
                continue
            if count is not None and done >= count:
                break
            time.sleep(interval_h * 3600.0)
    except KeyboardInterrupt:
        _log(log_path, f"stopped by user after {done} captures")
    finally:
        _release_awake()
    _log(log_path, f"collect done: {done} captures")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Unattended benign-capture loop: reset the board, capture, sleep, repeat.")
    ap.add_argument("variant", help="campaign tag for filenames + manifest, e.g. nv45s-lab-w1")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="hours between captures (default 2.0; ring turnover at 45 s is ~3.1 h)")
    ap.add_argument("--count", type=int, default=None,
                    help="stop after N captures (default: run until Ctrl+C)")
    ap.add_argument("--fresh", action="store_true",
                    help="erase the NV ring first; captures then walk the fill states")
    ap.add_argument("--port", default="COM3", help="serial port (ST-LINK VCP); default COM3")
    ap.add_argument("--baud", type=int, default=921600, help="must match the firmware (921600)")
    ap.add_argument("--cli", type=Path, default=DEFAULT_CLI,
                    help="path to STM32_Programmer_CLI.exe")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_CAPTURES_DIR,
                    help="dir holding the manifest + .bin files")
    args = ap.parse_args()
    return run(args.cli, args.port, args.baud, args.variant, args.out_dir,
               args.interval, args.count, args.fresh)


if __name__ == "__main__":
    raise SystemExit(main())
