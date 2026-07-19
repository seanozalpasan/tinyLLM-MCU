"""
Tail-burst collection: bank the late-fill ring states the model under-covers.

The session-09 watchdog bench proved the benign score climbs as a page fills
(the ring-cycle gradient) and crosses the alarm line in the page's last ~20
records -- a fill state the training set samples too thinly for the threshold
quantile to protect. This collector aims every capture at exactly that corner:
it parses each capture it just took (campaign.py's closed-loop trick), sleeps
out the well-covered early/mid fill so those records accumulate monotonically
exactly as deployment writes them, then bursts captures through the page's
last ~22 records until rotation. No firmware change and no reflash: the armed
IDS may watchdog-reset the board inside the band, but a reset never touches
flash, the boot-window dump precedes the first scan by design, and the loop
re-times from every capture -- so an IDS reset costs nothing but the same
timestamp-restart texture any capture reset already adds.

Run (repo root, venv active; the board on its deploy DUMP_NSFLASH=2 build;
CoolTerm CLOSED first -- it holds the COM port otherwise):
    python -m offdevice.data.tail_burst nv15s-lab-tail1 --hours 3
Ctrl+C stops cleanly; the manifest is appended per capture, so nothing
already banked is ever lost.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from offdevice.data.campaign import (
    MIN_CAPTURE_GAP_S,
    PAGE_RECORDS,
    CampaignError,
    Ctx,
    capture_with_retry,
    has_foreign_page,
    region_of,
    ring_state,
    seconds_until_used,
)
from offdevice.data.capture import DEFAULT_CAPTURES_DIR, VARIANT_RE
from offdevice.data.collect import (
    DEFAULT_CLI,
    LOG_NAME,
    _keep_awake,
    _log,
    _release_awake,
)

# ---- burst policy ----------------------------------------------------------------

# Where the burst begins, in used slots of the current page. The bench put the
# ramp's alarm crossings at ~slot 112+; starting at 100 banks the approach
# states too and leaves a 22-record cushion for wait-timing drift (a landing
# short just costs one extra mid-fill capture; the loop re-aims immediately).
TAIL_START_USED = 100

# Spacing inside the burst = the campaign's anti-clone floor (>= 2 fresh
# records between captures, so byte-identical images are structurally
# impossible): ~7 captures across the ~5.5 min the tail lasts each cycle.
BURST_SPACING_S = MIN_CAPTURE_GAP_S


def run_tail_burst(ctx: Ctx, tag: str, hours: float) -> int:
    """Seed, then alternate wait-to-tail and burst-through-tail until the cap."""
    t_end = time.monotonic() + hours * 3600.0
    tail_banked = 0

    _keep_awake()
    try:
        last = capture_with_retry(ctx, tag)
        while True:
            rv = region_of(last[0])
            if has_foreign_page(rv):
                raise CampaignError(
                    "capture holds a foreign page -- not a benign ring; investigate "
                    "before collecting anything else")
            if rv.current is None:
                raise CampaignError(
                    "ring is blank -- wrong board state for a tail run (no --fresh "
                    "erase belongs anywhere near this collector)")
            used, total, _ = ring_state(rv)

            elapsed = time.monotonic() - last[1]
            if used >= TAIL_START_USED:
                tail_banked += 1
                _log(ctx.log, f"tail capture banked: used={used}/{PAGE_RECORDS} "
                              f"total={total} (tail count {tail_banked})")
                wait = max(BURST_SPACING_S - elapsed, 0.0)
            else:
                _log(ctx.log, f"positioning capture: used={used}/{PAGE_RECORDS} "
                              f"total={total}; waiting for the tail")
                wait = max(seconds_until_used(used, TAIL_START_USED, elapsed),
                           MIN_CAPTURE_GAP_S - elapsed, 0.0)
                if wait > 60.0:
                    _log(ctx.log, f"sleeping {wait / 60.0:.1f} min until used~{TAIL_START_USED}")

            if time.monotonic() + wait >= t_end:
                _log(ctx.log, f"time budget reached -- done ({tail_banked} tail captures)")
                return 0
            time.sleep(wait)
            last = capture_with_retry(ctx, tag)
    except KeyboardInterrupt:
        _log(ctx.log, f"stopped by user ({tail_banked} tail captures banked)")
        return 0
    except CampaignError as exc:
        _log(ctx.log, f"ABORTED -- {exc}")
        return 1
    finally:
        _release_awake()


def main() -> int:
    """CLI wrapper; argument names mirror collect.py's."""
    ap = argparse.ArgumentParser(
        description="Tail-burst collection: wait out each page cycle, then capture "
                    "every ~45 s through the late-fill band until rotation.")
    ap.add_argument("variant", help="campaign tag for filenames + manifest, e.g. nv15s-lab-tail1")
    ap.add_argument("--hours", type=float, default=3.0,
                    help="total run length in hours (default 3.0; ~5 page cycles, "
                         "~35-40 tail captures)")
    ap.add_argument("--port", default="COM3", help="serial port (ST-LINK VCP); default COM3")
    ap.add_argument("--baud", type=int, default=921600, help="must match the firmware (921600)")
    ap.add_argument("--cli", type=Path, default=DEFAULT_CLI,
                    help="path to STM32_Programmer_CLI.exe")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_CAPTURES_DIR,
                    help="dir holding the manifest + .bin files")
    args = ap.parse_args()

    if VARIANT_RE.fullmatch(args.variant) is None:
        print(f"[tail_burst] variant tag must be [A-Za-z0-9.+-], got {args.variant!r}")
        return 2
    if args.hours <= 0:
        print(f"[tail_burst] --hours must be positive, got {args.hours}")
        return 2
    if not args.cli.exists():
        print(f"[tail_burst] STM32_Programmer_CLI not found at {args.cli} -- pass --cli")
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(cli=args.cli, port=args.port, baud=args.baud,
              captures_dir=args.out_dir, log=args.out_dir / LOG_NAME)
    return run_tail_burst(ctx, args.variant, args.hours)


if __name__ == "__main__":
    raise SystemExit(main())
