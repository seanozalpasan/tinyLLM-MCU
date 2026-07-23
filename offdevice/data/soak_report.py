"""
Soak-log analyzer: turn a console_log.py transcript into the soak's verdict.

Read-only companion to console_log.py: it parses a finished soak log and
reports everything the eval write-up needs — scan/alarm/reboot counts, the
benign score profile against the alarm line, ring rotations, scan-cadence
gaps, and an hourly table that pairs the score ceiling with the temperature
and pressure the room actually did (the NS telemetry lines carry T/RH/P, so
a long soak doubles as an environmental-breadth record). The console echoes
whatever display units are selected (degF/inHg after a B2 toggle), but the
units change what the device says, never what the room did — so all four
unit combinations are parsed and every range is reported in canonical
degC/hPa, with per-unit line counts shown so nothing is silently converted
away. Exit code 0 means
the pre-registered bar held: no ANOMALY lines, no mid-run reboots, and every
score under the threshold.

    python -m offdevice.data.soak_report docs\\soaks\\soak24h.txt
    python -m offdevice.data.soak_report soak.txt --threshold 13.909
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_THRESHOLD = 13.909  # the shipped 1%-FP alarm line (model artifact)

# console_log.py stamps "[+MM:SS]"; the minutes field grows past two digits
# on long runs, so the width is open-ended.
_STAMP_RE = re.compile(r"^\[\+(\d+):(\d{2})\]")
_SCAN_RE = re.compile(
    r"\[IDS\] scan #(\d+) score=(\d+\.\d+)"
    r"(?:.*?slot=(\d+|--)/\d+ seq=(\d+|--))?")
_NS_RE = re.compile(
    r"\[NS\] .*T=(-?\d+\.\d+)(C|F) .*P=(\d+\.\d+)(hPa|inHg)")
_BOOT_MARK = "[IDS] scan tick armed"  # printed exactly once per boot

INHG_TO_HPA = 33.86389  # conventional inch of mercury, 3386.389 Pa (NIST)


@dataclass
class Scan:
    """One parsed [IDS] scan line."""
    elapsed_s: int
    number: int
    score: float
    slot: int | None
    seq: int | None
    anomaly: bool


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile; enough precision for a soak summary."""
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _fmt_hms(seconds: int) -> str:
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Summarize a console_log.py soak transcript (read-only).")
    ap.add_argument("log", type=Path)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"alarm line to judge against (default {DEFAULT_THRESHOLD})")
    args = ap.parse_args()

    scans: list[Scan] = []
    env: list[tuple[int, float, float]] = []  # (elapsed_s, temp degC, pressure hPa)
    n_lines = 0
    n_boot_marks = 0
    n_failed = 0     # non-finite score lines: withheld kick without a score
    n_transient = 0  # one-rescan transient guard lines that ended benign
    n_temp_f = 0     # telemetry lines that arrived in degF (converted)
    n_press_inhg = 0  # telemetry lines that arrived in inHg (converted)

    for line in args.log.read_text(encoding="utf-8").splitlines():
        n_lines += 1
        m = _STAMP_RE.match(line)
        if not m:
            continue
        elapsed = int(m.group(1)) * 60 + int(m.group(2))

        if _BOOT_MARK in line:
            n_boot_marks += 1
        if "FAILED (non-finite" in line:
            n_failed += 1
        if "on rescan" in line and "ANOMALY" not in line:
            n_transient += 1

        s = _SCAN_RE.search(line)
        if s:
            slot_raw, seq_raw = s.group(3), s.group(4)
            scans.append(Scan(
                elapsed_s=elapsed,
                number=int(s.group(1)),
                score=float(s.group(2)),
                slot=None if slot_raw in (None, "--") else int(slot_raw),
                seq=None if seq_raw in (None, "--") else int(seq_raw),
                anomaly="ANOMALY" in line,
            ))
            continue
        e = _NS_RE.search(line)
        if e:
            temp = float(e.group(1))
            if e.group(2) == "F":
                temp = (temp - 32.0) * 5.0 / 9.0
                n_temp_f += 1
            press = float(e.group(3))
            if e.group(4) == "inHg":
                press *= INHG_TO_HPA
                n_press_inhg += 1
            env.append((elapsed, temp, press))

    if not scans:
        print(f"[soak] {args.log}: no [IDS] scan lines found ({n_lines} lines)")
        return 1

    duration = scans[-1].elapsed_s - scans[0].elapsed_s
    scores = sorted(sc.score for sc in scans)
    anomalies = [sc for sc in scans if sc.anomaly]
    over_line = [sc for sc in scans if sc.score >= args.threshold]

    # A reboot mid-run shows up two independent ways: an extra boot banner,
    # and the secure scan counter starting over. Report both; they should
    # agree unless the log started mid-boot.
    restarts = sum(1 for a, b in zip(scans, scans[1:]) if b.number <= a.number)
    rotations = sum(1 for a, b in zip(scans, scans[1:])
                    if a.seq is not None and b.seq is not None and b.seq != a.seq)
    max_gap = max((b.elapsed_s - a.elapsed_s for a, b in zip(scans, scans[1:])),
                  default=0)

    print(f"[soak] {args.log.name}: {_fmt_hms(duration)} covered, "
          f"{len(scans)} scans, {rotations} ring rotations")
    print(f"[soak] anomalies: {len(anomalies)}   scans >= {args.threshold}: "
          f"{len(over_line)}   non-finite: {n_failed}   "
          f"rescan transients (benign): {n_transient}")
    print(f"[soak] reboots: {max(n_boot_marks - 1, 0)} extra boot banners, "
          f"{restarts} scan-counter restarts")
    print(f"[soak] scores: min {scores[0]:.3f}  median "
          f"{_percentile(scores, 0.50):.3f}  p95 {_percentile(scores, 0.95):.3f}  "
          f"p99 {_percentile(scores, 0.99):.3f}  max {scores[-1]:.3f}  "
          f"(margin to line: {args.threshold - scores[-1]:+.3f})")
    print(f"[soak] max scan gap: {max_gap}s (cadence nominal 25s)")
    if env:
        print(f"[soak] telemetry: {len(env)} lines "
              f"(T: {len(env) - n_temp_f} degC + {n_temp_f} degF; "
              f"P: {len(env) - n_press_inhg} hPa + {n_press_inhg} inHg) "
              f"-- ranges below in canonical degC/hPa")

    print("\n[soak] top 5 scores:")
    for sc in sorted(scans, key=lambda s: s.score, reverse=True)[:5]:
        pos = f"slot={sc.slot}/122 seq={sc.seq}" if sc.slot is not None else "slot=--"
        print(f"    {sc.score:7.3f}  at +{_fmt_hms(sc.elapsed_s)}  {pos}")

    print("\n[soak] hour  scans  max-score   T range (degC)   P range (hPa)")
    for h in range(duration // 3600 + 1):
        lo, hi = h * 3600, (h + 1) * 3600
        hr_scans = [sc for sc in scans if lo <= sc.elapsed_s < hi]
        hr_env = [e for e in env if lo <= e[0] < hi]
        if not hr_scans and not hr_env:
            continue
        smax = f"{max(sc.score for sc in hr_scans):9.3f}" if hr_scans else "        -"
        if hr_env:
            ts, ps = [e[1] for e in hr_env], [e[2] for e in hr_env]
            trange = f"{min(ts):6.2f}..{max(ts):6.2f}"
            prange = f"{min(ps):7.2f}..{max(ps):7.2f}"
        else:
            trange, prange = "      -", "       -"
        print(f"    {h:3d}  {len(hr_scans):5d}  {smax}   {trange}   {prange}")

    clean = (not anomalies and not over_line and restarts == 0
             and n_boot_marks <= 1)
    print(f"\n[soak] verdict: {'CLEAN — pre-registered bar held' if clean else 'NOT CLEAN — see counts above'}")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
