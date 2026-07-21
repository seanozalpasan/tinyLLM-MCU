"""
Honest-tail collection: one capture per page cycle, tail written uninterrupted.

tail_burst.py banked the late-fill band by capturing every ~45 s through it --
and every capture resets the board (the dump runs in the boot window so the
ring is frozen mid-transfer), so those captures' last ~20 records carry a
15/30 timestamp-restart lattice that steady deployment never writes: the live
tail climbs monotonically (e.g. 1500..1830). The model read those timestamp
bytes as part of the benign corner, and live near-full pages still overshoot
their trained analogs by the ~1.5-2 points that cross the alarm line. This
collector fixes the distribution: ONE capture per page cycle, aimed at a late
slot, so every banked tail was written in an unbroken run exactly as
deployment writes it. Yield is ~2 captures/hour -- the price of honesty; a
page takes 30.5 minutes to fill.

The board must run the DISARMED collection build: IDS_SCAN_ARMED 0 in
Secure/Core/Inc/ids_scan.h (Secure-only rebuild + reflash; flip back to 1
before any soak or demo). Disarming removes the one self-reset source, so
pages fill uninterrupted AND the capture targets can sit inside the alarm
crossing band itself -- the deepest corner states. The scan never writes the
NV region, so disarmed-build benign data is byte-identical to deploy-build
benign data.

Every capture is still verified after banking: the current page's trailing
records must carry strictly climbing timestamps -- no reboot seam. A capture
that fails -- an unplanned reset (capture retry, power blip) landed mid-fill
-- is loudly flagged HONESTY FAIL here and in the end-of-run summary; list
those names in offdevice/data/quarantine.txt before any re-fit. One residue
is accepted and disclosed: the capture's own reset seams the last 2-5 slots
of the page it interrupts, which reappear one page later as the next
capture's PREVIOUS page -- the irreducible cost of a boot-window dump,
several times smaller than the artifact this collector removes, and the same
pattern deployment writes after any real reboot.

Run (repo root, venv active; board on the DISARMED DUMP_NSFLASH=2 build;
CoolTerm and console_log.py CLOSED -- one COM-port owner at a time):
    python -m offdevice.data.tail_honest nv15s-lab-tail2 --hours 4.5
Ctrl+C stops cleanly; every capture taken is already banked + manifested.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from offdevice.data.campaign import (
    PAGE_RECORDS,
    PERIOD_S,
    CampaignError,
    Ctx,
    capture_with_retry,
    has_foreign_page,
    region_of,
    ring_state,
)
from offdevice.data.capture import DEFAULT_CAPTURES_DIR, VARIANT_RE
from offdevice.data.collect import (
    DEFAULT_CLI,
    LOG_NAME,
    _keep_awake,
    _log,
    _release_awake,
)

# ---- aim + honesty policy ----------------------------------------------------------

# Late-slot aim points, rotated per cycle. The collection build is DISARMED
# (no watchdog to race), so the targets sit inside the crossing band itself:
# 114 and 120 are the very slots live alarms fired at, and 113-115 has grazed
# the line. Aiming past 120 risks drifting over the rotation edge and wasting
# a cycle, so the set varies depth while keeping a 2-slot cushion.
TAIL_TARGETS = (114, 117, 119, 120)

# The live alarm ramp lives in the page's last ~20 records, so a reboot seam
# anywhere in a banked tail's last 20 records disqualifies it. The check reads
# one extra record so the seam PAIR at the window's edge is still compared.
HONEST_WINDOW = 20

# A capture must sit at least this deep to count as a tail sample at all;
# anything shallower is a positioning capture (banked, benign, just not tail).
TAIL_MIN_USED = 105


def tail_is_honest(records: list[dict[str, int]]) -> bool:
    """True when the trailing HONEST_WINDOW records show no timestamp restart.

    Timestamps count seconds since boot and climb one record period at a time,
    so any non-increase inside the window is a reboot seam. The slice takes one
    record beyond the window so the boundary pair is compared too.
    """
    ts = [r["ts"] for r in records[-(HONEST_WINDOW + 1):]]
    return all(b > a for a, b in zip(ts, ts[1:]))


def run_tail_honest(ctx: Ctx, tag: str, hours: float) -> int:
    """Capture, verify, aim the next cycle's slot, sleep -- until the budget."""
    t_end = time.monotonic() + hours * 3600.0
    honest = 0
    failed: list[str] = []
    cycle = 0

    _keep_awake()
    try:
        last = capture_with_retry(ctx, tag)
        while True:
            path, t_frozen = last
            rv = region_of(path)
            if has_foreign_page(rv):
                raise CampaignError(
                    "capture holds a foreign page -- not a benign ring; investigate "
                    "before collecting anything else")
            if rv.current is None:
                raise CampaignError(
                    "ring is blank -- wrong board state for a tail run (no --fresh "
                    "erase belongs anywhere near this collector)")
            used, total, _ = ring_state(rv)

            if used >= TAIL_MIN_USED:
                if tail_is_honest(rv.pages[rv.current].records):
                    honest += 1
                    _log(ctx.log, f"honest tail banked: used={used}/{PAGE_RECORDS} "
                                  f"total={total} (count {honest})")
                else:
                    failed.append(path.name)
                    _log(ctx.log, f"HONESTY FAIL: used={used} but a reboot seam sits "
                                  f"in the last {HONEST_WINDOW} records -- quarantine "
                                  f"{path.name}")
            else:
                _log(ctx.log, f"positioning capture: used={used}/{PAGE_RECORDS} "
                              f"total={total}")

            target = TAIL_TARGETS[cycle % len(TAIL_TARGETS)]
            cycle += 1
            # Always aim the NEXT page: this capture's own reset just seamed
            # the current page at ~slot used+1, so only a page that opens after
            # the coming rotation fills clean from slot 0. A same-page shortcut
            # would bank a small-timestamp post-reboot tail -- diluting the
            # steady-running pattern this run exists to collect.
            slots = (PAGE_RECORDS - used) + target
            elapsed = time.monotonic() - t_frozen
            wait = max(slots * PERIOD_S - elapsed, 0.0)

            if time.monotonic() + wait >= t_end:
                _log(ctx.log, f"time budget reached -- done ({honest} honest tails, "
                              f"{len(failed)} honesty fails)")
                break
            if wait > 60.0:
                _log(ctx.log, f"sleeping {wait / 60.0:.1f} min to aim slot ~{target}")
            time.sleep(wait)
            last = capture_with_retry(ctx, tag)
    except KeyboardInterrupt:
        _log(ctx.log, f"stopped by user ({honest} honest tails, "
                      f"{len(failed)} honesty fails)")
    except CampaignError as exc:
        _log(ctx.log, f"ABORTED -- {exc}")
        return 1
    finally:
        _release_awake()

    if failed:
        _log(ctx.log, "quarantine these before any re-fit: " + ", ".join(failed))
    return 0


def main() -> int:
    """CLI wrapper; argument names mirror collect.py's."""
    ap = argparse.ArgumentParser(
        description="Honest-tail collection: one capture per page cycle at a late "
                    "slot, the tail written uninterrupted as deployment writes it.")
    ap.add_argument("variant", help="campaign tag for filenames + manifest, e.g. nv15s-lab-tail2")
    ap.add_argument("--hours", type=float, default=4.5,
                    help="total run length in hours (default 4.5; ~2 captures/hour)")
    ap.add_argument("--port", default="COM3", help="serial port (ST-LINK VCP); default COM3")
    ap.add_argument("--baud", type=int, default=921600, help="must match the firmware (921600)")
    ap.add_argument("--cli", type=Path, default=DEFAULT_CLI,
                    help="path to STM32_Programmer_CLI.exe")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_CAPTURES_DIR,
                    help="dir holding the manifest + .bin files")
    args = ap.parse_args()

    if VARIANT_RE.fullmatch(args.variant) is None:
        print(f"[tail_honest] variant tag must be [A-Za-z0-9.+-], got {args.variant!r}")
        return 2
    if args.hours <= 0:
        print(f"[tail_honest] --hours must be positive, got {args.hours}")
        return 2
    if not args.cli.exists():
        print(f"[tail_honest] STM32_Programmer_CLI not found at {args.cli} -- pass --cli")
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(cli=args.cli, port=args.port, baud=args.baud,
              captures_dir=args.out_dir, log=args.out_dir / LOG_NAME)
    return run_tail_honest(ctx, args.variant, args.hours)


if __name__ == "__main__":
    raise SystemExit(main())
