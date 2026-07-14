"""
One-command v2 campaign orchestrator: the whole multi-day collection plan,
unattended -- fill walk, near-empty top-up, steady loop, and timed
cycle-extreme chains -- so nobody stops and starts collectors by hand.

Built on collect.py's primitives (reset/erase via STM32_Programmer_CLI, the
boot-window capture, the append-only manifest) plus nv/parse.py, which is what
makes the rare fill states automatable: the orchestrator parses each capture
it just took, so it knows the ring's exact position and the capture's exact
moment. After the first wrap the ring's record total cycles 123..244 once per
page cycle (122 records x the record period), and the benign manifold is
steepest and least-sampled in the 238..244 band (~90 s per cycle) -- each
chain leg aims at an explicit slot target and re-times from the capture it
just parsed, so the legs walk the band bottom-to-top and end just past the
rotation, and no timing error can compound from one leg into the next.

Run (lab desktop, repo root, venv active; board on a DUMP_NSFLASH=2 build):
    python -m offdevice.data.campaign --hours 71
Resume after a crash (skips the fill phases; chain offsets count from the NEW
start; omit --chain-at entirely to keep the two defaults, or pass one per
remaining chain):
    python -m offdevice.data.campaign --hours 40 --skip-fill --chain-at 12
"""

from __future__ import annotations

import argparse
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import serial  # pyserial

from offdevice.data.capture import (
    DEFAULT_CAPTURES_DIR,
    VARIANT_RE,
    CaptureError,
)
from offdevice.data.collect import (
    DEFAULT_CLI,
    ERASE_ARGS,
    LOG_NAME,
    MAX_CONSECUTIVE_FAILURES,
    RESET_ARGS,
    RETRY_DELAY_S,
    CollectError,
    _keep_awake,
    _log,
    _release_awake,
    _run_cli,
    collect_once,
)
from offdevice.nv import spec
from offdevice.nv.parse import RegionView, parse_region, slice_nv

# ---- schedule constants (all derived from the spec's deploy rate) ----------------

PERIOD_S = float(spec.RATE_DEPLOY_PERIOD_S)          # 15 s between records
PAGE_RECORDS = spec.RECORDS_PER_PAGE                 # 122 records per page
PAGE_CYCLE_S = PAGE_RECORDS * PERIOD_S               # 30.5 min per page cycle
BAND_LO = spec.RECORDS_TOTAL - 6                     # 238..244 = the steep band
BAND_HI = spec.RECORDS_TOTAL

FILL_COUNT = 9                    # empty + walk + just-wrapped (ring fills in ~61 min)
FILL_INTERVAL_S = 9 * 60.0

# Near-empty top-up: sequential waits AFTER the fresh erase, so the two captures
# land at ~420 s (~27 records) and ~870 s (~57 records) -- two distinct
# near-empty (<= 61 records) samples, and NO capture at t=0: the fill walk
# already banked the campaign's single empty capture, and a second empty would
# be its byte-identical clone.
TOPUP_DELAYS_S = (420.0, 450.0)

# Cycle-extreme chain: explicit used-slot targets, one leg per page cycle. The
# first four walk the 238..244-record band bottom-to-top (used 116..122 once
# the ring has wrapped); the last is the just-after-rotation state, the
# cycle's other extreme. Aiming starts at the band's BOTTOM because landings
# run high, not low: real drift beats the pure 15 s/record model (each capture
# reboot re-phases the record clock, and the tick behind it is an RC-derived
# HAL millisecond), measured at ~+2 records per leg -- and +2 past any target
# below still lands a band, full-ring, or just-after-rotation state.
CHAIN_TARGETS_USED = (116, 118, 120, 122, 4)

# GOTCHA: two captures freezing inside one record period hold the same ring
# image -- a byte-identical clone that double-weights training and leaks
# holdout questions (it has happened twice, both at phase seams). Three record
# periods of forced spacing keeps it structurally impossible.
MIN_CAPTURE_GAP_S = 3.0 * PERIOD_S

_RETRYABLE = (CaptureError, CollectError, serial.SerialException, subprocess.TimeoutExpired)


class CampaignError(RuntimeError):
    """A phase failed in a way that needs a human decision -- stop the campaign."""


@dataclass(frozen=True)
class Ctx:
    """Everything a capture cycle needs, threaded through the phases."""

    cli: Path
    port: str
    baud: int
    captures_dir: Path
    log: Path


# ---- pure ring math (unit-tested; no I/O) -----------------------------------------


def seconds_until_used(used_slots: int, target_used: int, elapsed_s: float,
                       min_wait_s: float = 0.0) -> float:
    """Wall-clock wait until the current page should hold target_used slots.

    used_slots comes from a capture parsed elapsed_s ago; one record lands every
    PERIOD_S while the board runs. A wait under min_wait_s rolls forward whole
    page cycles: chain legs pass half a page cycle there, so consecutive legs
    sample different page contents rather than near-identical neighbors from
    the same cycle.
    """
    wait = ((target_used - used_slots) % PAGE_RECORDS) * PERIOD_S - elapsed_s
    while wait < min_wait_s:
        wait += PAGE_CYCLE_S
    return wait


def ring_state(rv: RegionView) -> tuple[int, int, bool]:
    """(current-page used slots, total records, post-wrap?) of a parsed region.

    Post-wrap means the non-current page is a full valid page -- from then on
    the total cycles 123..244 per page cycle and the band math above is valid.
    """
    if rv.current is None:
        return 0, 0, False
    cur = rv.pages[rv.current]
    other = rv.pages[1 - rv.current]
    other_full = other.header is not None and len(other.records) == PAGE_RECORDS
    total = len(cur.records) + len(other.records)
    return len(cur.records), total, other_full


def region_of(bin_path: Path) -> RegionView:
    """Parse the NV region out of a saved 256 KB capture."""
    return parse_region(slice_nv(bin_path.read_bytes()))


def has_foreign_page(rv: RegionView) -> bool:
    """True when any page is neither blank nor a valid ring page."""
    return any(p.header is None and not p.blank for p in rv.pages)


# ---- capture / erase wrappers ------------------------------------------------------


def fresh_erase(ctx: Ctx) -> None:
    """Reset -> erase the NV pages -> reset (collect.py's race-safe bracket)."""
    _run_cli(ctx.cli, RESET_ARGS)
    _run_cli(ctx.cli, ERASE_ARGS)
    _run_cli(ctx.cli, RESET_ARGS)
    _log(ctx.log, "campaign: NV ring erased (virgin ring)")


def capture_with_retry(ctx: Ctx, tag: str) -> tuple[Path, float]:
    """One capture under the unattended-run policy; returns (path, reset moment).

    The monotonic timestamp is taken just before the cycle's board reset, which
    is the moment the returned dump's ring state was frozen -- the anchor every
    band-timing computation counts from.
    """
    failures = 0
    while True:
        t0 = time.monotonic()
        try:
            path = collect_once(ctx.cli, ctx.port, ctx.baud, tag, ctx.captures_dir)
            _log(ctx.log, f"campaign: capture ok tag={tag}: {path.name}")
            return path, t0
        except _RETRYABLE as exc:
            failures += 1
            _log(ctx.log, f"campaign: capture FAILED ({failures}/{MAX_CONSECUTIVE_FAILURES}) "
                          f"tag={tag}: {exc}")
            if failures >= MAX_CONSECUTIVE_FAILURES:
                raise CampaignError("consecutive-failure limit reached; see collect.log") from exc
            time.sleep(RETRY_DELAY_S)


# ---- phases ------------------------------------------------------------------------


def run_fill_walk(ctx: Ctx, prefix: str) -> tuple[Path, float]:
    """Fresh erase, then FILL_COUNT captures 9 min apart: empty -> ... -> wrapped.

    Capture 1 fires immediately after the erase and is the campaign's ONE empty
    capture (the dump precedes the workload every boot, so its ring is blank by
    construction). Captures 1 and 2 are auto-verified: the erase bracket's quiet
    window is only one record period, and a lost race poisons the whole chain.
    """
    tag = f"{prefix}-fill1"
    _log(ctx.log, f"campaign: fill walk begins ({FILL_COUNT} captures, "
                  f"{FILL_INTERVAL_S / 60:.0f} min apart, tag={tag})")

    # The empty capture must actually be empty. A foreign page means the erase
    # raced the logger (human decision: quarantine the chain); a VALID page with
    # a few records just means a slow cycle let the logger tick first -- erase
    # again and retake, so the campaign still banks its one true empty ring.
    last: tuple[Path, float] | None = None
    for attempt in range(2):
        fresh_erase(ctx)
        last = capture_with_retry(ctx, tag)
        rv = region_of(last[0])
        if all(p.blank for p in rv.pages):
            break
        if has_foreign_page(rv):
            raise CampaignError(
                "fill capture 1 has a foreign page -- the erase raced the logger. "
                "Re-run with a fresh tag prefix and quarantine this chain's captures.")
        _log(ctx.log, "campaign: fill capture 1 was not empty (slow cycle let the "
                      "logger tick first) -- erasing and retaking it")
        if attempt == 1:
            raise CampaignError(
                "fill capture 1 still not empty after a retry -- investigate before "
                "spending campaign time. Quarantine this chain's captures.")

    for i in range(1, FILL_COUNT):
        time.sleep(FILL_INTERVAL_S)
        last = capture_with_retry(ctx, tag)
        rv = region_of(last[0])
        if i == 1:
            if has_foreign_page(rv) or rv.current is None:
                raise CampaignError(
                    "fill capture 2 has a foreign/invalid page -- the erase raced the "
                    "logger. Re-run with a fresh tag prefix and quarantine this chain.")
            _, total, _ = ring_state(rv)
            _log(ctx.log, f"campaign: fill verified clean (capture 2 holds {total} records)")
    assert last is not None
    return last


def run_topup(ctx: Ctx, prefix: str) -> tuple[Path, float]:
    """Fresh erase, then two DELAYED captures -- distinct near-empty samples.

    The splitter keeps single-capture strata entirely in training, so the
    near-empty stratum needs at least two distinct captures to be examinable;
    the fill walk contributes one more. The last capture is returned as the
    scheduling seed for whatever runs next.
    """
    tag = f"{prefix}-fill2"
    _log(ctx.log, f"campaign: near-empty top-up begins (tag={tag})")
    fresh_erase(ctx)
    last: tuple[Path, float] | None = None
    for delay in TOPUP_DELAYS_S:
        time.sleep(delay)
        last = capture_with_retry(ctx, tag)
        _, total, _ = ring_state(region_of(last[0]))
        note = "near-empty" if total <= 61 else "WARNING: past the near-empty line (61)"
        _log(ctx.log, f"campaign: top-up capture holds {total} records ({note})")
    assert last is not None
    return last


def run_chain(ctx: Ctx, tag: str, newest: tuple[Path, float]) -> tuple[Path, float]:
    """One cycle-extreme chain: a capture per target slot, re-timed leg by leg."""
    used, total, post_wrap = ring_state(region_of(newest[0]))
    if not post_wrap:
        _log(ctx.log, f"campaign: chain {tag} SKIPPED -- ring has not wrapped yet "
                      f"(total={total}); steady sampling continues")
        return newest
    last = newest
    for i, target in enumerate(CHAIN_TARGETS_USED):
        min_wait = 0.0 if i == 0 else PAGE_CYCLE_S / 2.0
        elapsed = time.monotonic() - last[1]
        wait = max(seconds_until_used(used, target, elapsed, min_wait),
                   MIN_CAPTURE_GAP_S - elapsed)
        _log(ctx.log, f"campaign: chain {tag} leg {i + 1}/{len(CHAIN_TARGETS_USED)} "
                      f"waiting {wait / 60:.1f} min to reach used={target} "
                      f"(band {BAND_LO}..{BAND_HI}; extrapolated from used={used})")
        time.sleep(wait)
        last = capture_with_retry(ctx, tag)
        used, chain_total, _ = ring_state(region_of(last[0]))
        _log(ctx.log, f"campaign: chain {tag} capture {i + 1}/{len(CHAIN_TARGETS_USED)}: "
                      f"total={chain_total} (target band {BAND_LO}..{BAND_HI})")
    return last


# ---- the campaign ------------------------------------------------------------------


def run_campaign(ctx: Ctx, prefix: str, hours: float, steady_interval_s: float,
                 chain_at_h: list[float], skip_fill: bool) -> int:
    """The whole plan; returns a process exit code."""
    t_start = time.monotonic()
    t_end = t_start + hours * 3600.0
    chains = sorted((t_start + h * 3600.0, f"{prefix}-top{i + 1}")
                    for i, h in enumerate(sorted(chain_at_h)))
    steady_tag = f"{prefix}-steady1"

    eta = datetime.now() + timedelta(hours=hours)
    _log(ctx.log, f"campaign: start (prefix={prefix}, {hours:.0f} h, ends ~{eta:%a %H:%M}); "
                  f"plan = {'(fill skipped) ' if skip_fill else 'fill walk + top-up, '}"
                  f"steady every {steady_interval_s / 60:.0f} min, "
                  f"{len(chains)} cycle-extreme chain(s) at "
                  f"{', '.join(f'+{h:.0f}h' for h in sorted(chain_at_h)) or 'none'}")

    _keep_awake()
    try:
        if skip_fill:
            newest = capture_with_retry(ctx, steady_tag)
        else:
            run_fill_walk(ctx, prefix)
            # The top-up's last capture seeds the steady loop and the chain
            # math -- a fresh seed capture here would re-freeze the same ring
            # within seconds of it: a byte-identical clone.
            newest = run_topup(ctx, prefix)
        next_steady = time.monotonic() + steady_interval_s

        while True:
            now = time.monotonic()
            if now >= t_end:
                _log(ctx.log, "campaign: time budget reached -- done")
                return 0
            if chains and now >= chains[0][0]:
                _, tag = chains.pop(0)
                newest = run_chain(ctx, tag, newest)
                next_steady = time.monotonic() + steady_interval_s
                continue
            if now >= next_steady:
                newest = capture_with_retry(ctx, steady_tag)
                next_steady = time.monotonic() + steady_interval_s
                continue
            next_wake = min(t_end, next_steady, chains[0][0] if chains else t_end)
            time.sleep(max(1.0, next_wake - time.monotonic()))
    except KeyboardInterrupt:
        _log(ctx.log, "campaign: stopped by user")
        return 0
    except CampaignError as exc:
        _log(ctx.log, f"campaign: ABORTED -- {exc}")
        return 1
    finally:
        _release_awake()


def main() -> int:
    """CLI wrapper; argument names mirror collect.py's."""
    ap = argparse.ArgumentParser(
        description="Unattended v2 campaign: fill walk + near-empty top-up + steady "
                    "loop + timed cycle-extreme chains, in one run.")
    ap.add_argument("--hours", type=float, default=71.0,
                    help="total campaign length in hours (default 71)")
    ap.add_argument("--prefix", default="nv15s-lab",
                    help="tag prefix; tags become <prefix>-fill1/-fill2/-steady1/-topN")
    ap.add_argument("--steady-interval", type=float, default=0.667,
                    help="steady-loop hours between captures (default 0.667 = 40 min)")
    ap.add_argument("--chain-at", type=float, action="append", default=None,
                    help="hours from start to run a cycle-extreme chain; repeatable "
                         "(default: 20 and 44); pass --chain-at -1 for none")
    ap.add_argument("--skip-fill", action="store_true",
                    help="resume mode: skip the fill walk + top-up phases")
    ap.add_argument("--port", default="COM3", help="serial port (ST-LINK VCP); default COM3")
    ap.add_argument("--baud", type=int, default=921600, help="must match the firmware (921600)")
    ap.add_argument("--cli", type=Path, default=DEFAULT_CLI,
                    help="path to STM32_Programmer_CLI.exe")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_CAPTURES_DIR,
                    help="dir holding the manifest + .bin files")
    args = ap.parse_args()

    if VARIANT_RE.fullmatch(f"{args.prefix}-steady1") is None:
        print(f"[campaign] prefix must be tag-safe [A-Za-z0-9.+-], got {args.prefix!r}")
        return 2
    if args.hours <= 0 or args.steady_interval <= 0:
        print("[campaign] --hours and --steady-interval must be positive")
        return 2
    chain_at = [20.0, 44.0] if args.chain_at is None else [h for h in args.chain_at if h >= 0]
    if any(h >= args.hours for h in chain_at):
        print("[campaign] every --chain-at must fall inside --hours")
        return 2
    if not args.cli.exists():
        print(f"[campaign] STM32_Programmer_CLI not found at {args.cli} -- pass --cli")
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(cli=args.cli, port=args.port, baud=args.baud,
              captures_dir=args.out_dir, log=args.out_dir / LOG_NAME)
    return run_campaign(ctx, args.prefix, args.hours, args.steady_interval * 3600.0,
                        chain_at, args.skip_fill)


if __name__ == "__main__":
    raise SystemExit(main())
