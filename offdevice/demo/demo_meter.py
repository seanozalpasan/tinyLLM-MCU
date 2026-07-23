"""
Poster-demo live score meter: the board's IDS scan trace as a cardiac monitor.

Bottom-half-of-the-screen display for the poster table: black screen, a bold
green trace of the on-chip anomaly score (one x-step per scan; a scan fires
every 25 s on the board), a bright red line at the model's alarm threshold,
and big readouts (current score, ring position, alarm count). An ANOMALY scan
turns the newest point red and raises a blinking banner; a board reboot shows
as a dotted vertical scar. Strictly read-only and board-blind: it never opens
the serial port -- it tails the text file offdevice.data.console_log is
already writing, so the console logger stays the port's only owner.

Replay mode streams a banked log with an amber REPLAY watermark and transport
controls: SPACE pause/play, UP/DOWN arrows speed, LEFT/RIGHT arrows jump, and
a scrub slider (dragging it pauses playback).

    # live (poster): console_log.py running in another terminal, then
    python -m offdevice.demo.demo_meter --log console_soak_20260722T090000.txt

    # rehearsal without hardware:
    python -m offdevice.demo.demo_meter --replay docs/soaks/exclusionSoak.txt
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import NamedTuple

import matplotlib

matplotlib.rcParams["toolbar"] = "None"   # kiosk look: no widget strip
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Slider

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_JSON = REPO_ROOT / "offdevice" / "model" / "artifacts" / "mahalanobis.json"
FALLBACK_THRESHOLD = 13.909   # deployed alarm line; used only if the json is gone

# ---- the look: stereotypical cardiac monitor, plus a red alarm line --------
BG = "#000000"
TRACE = "#00FF41"          # classic monitor green
TRACE_DIM = "#9be8b3"      # secondary readouts -- neutral, not the series color
GRID = "#00FF41"           # drawn at low alpha so it recedes
ALARM = "#FF2B2B"          # bright red: reserved for the threshold + anomalies
REPLAY_TAG = "#FFB000"     # amber: the "this is a recording" honesty layer

WINDOW = 48                # visible scans (~20 min of 25 s ticks)
TICK_MS = 100              # animation cadence; tailing + playback ride on it
REPLAY_EVENTS_PER_TICK = 0.2   # 1x speed ~= 2 scans/s on screen
SPEED_MIN, SPEED_MAX = 0.25, 32.0
JUMP = 25                  # LEFT/RIGHT arrow jump, in events

# One point per scan line. Old logs lack slot=/seq=, so those are optional;
# the verdict word is checked on the whole line (ANOMALY lines carry a tail).
SCAN_RE = re.compile(r"\[IDS\] scan #(\d+) .*?score=(\d+\.\d+)")
SLOT_RE = re.compile(r"slot=(\d+)/(\d+)")
SEQ_RE = re.compile(r"seq=(\d+)")
REBOOT_MARK = "[IDS] scan tick armed"     # printed once per boot
HASH_MARK = "*** MISMATCH ***"            # Part-1 tamper, boot-time print
DIRTY_MARK = "Part-1 static region DIRTY" # Part-1 tamper, per-scan print --
                                          # follows a "benign" Part-2 scan line,
                                          # so it must re-raise the banner

MSG_ANOM = "ANOMALY -- WATCHDOG KICK WITHHELD, RESET IMMINENT"
MSG_HASH = "STATIC-REGION HASH MISMATCH -- Part 1 tamper"
MSG_DIRTY = "PART-1 STATIC REGION DIRTY -- WATCHDOG KICK WITHHELD"


class Scan(NamedTuple):
    """One [IDS] scan line, parsed."""
    scan_no: int
    score: float
    anomalous: bool
    slot: int | None
    slot_max: int | None
    seq: int | None


def parse_event(line: str) -> Scan | str | None:
    """A console line -> a Scan, a marker string ('reboot'/'hash'/'dirty'), or None."""
    if REBOOT_MARK in line:
        return "reboot"
    if HASH_MARK in line:
        return "hash"
    if DIRTY_MARK in line:
        return "dirty"
    m = SCAN_RE.search(line)
    if not m:
        return None
    ms, mq = SLOT_RE.search(line), SEQ_RE.search(line)
    return Scan(scan_no=int(m.group(1)), score=float(m.group(2)),
                anomalous="ANOMALY" in line,
                slot=int(ms.group(1)) if ms else None,
                slot_max=int(ms.group(2)) if ms else None,
                seq=int(mq.group(1)) if mq else None)


def load_threshold() -> float:
    """Alarm line from the deployed model's metadata; never dies at the poster."""
    try:
        return float(json.loads(MODEL_JSON.read_text())["threshold"])
    except Exception:
        print(f"[meter] WARNING: {MODEL_JSON.name} unreadable -- "
              f"using {FALLBACK_THRESHOLD}")
        return FALLBACK_THRESHOLD


class Tail:
    """Live mode: yields new complete lines as the console log file grows."""

    def __init__(self, log: Path) -> None:
        self.log = log
        self._fh = None
        self._buf = ""

    def poll(self) -> list[str]:
        if self._fh is None:
            if not self.log.exists():
                return []                      # console_log not started yet
            self._fh = self.log.open("r", encoding="utf-8", errors="replace")
        # Manual buffering: a line is only consumed once its newline arrived,
        # so a partially flushed write can never produce a torn parse.
        self._buf += self._fh.read()
        if "\n" not in self._buf:
            return []
        *lines, self._buf = self._buf.split("\n")
        return lines


class Meter:
    """Event list + a playback cursor; artists rebuilt whenever the cursor moves.

    Live mode appends events and pins the cursor to the end; replay mode owns
    the full list up front and moves the cursor by transport controls. One
    render path serves both, so a replay scrub and a live tick can't drift.
    """

    def __init__(self, threshold: float, tail: Tail | None,
                 events: list[Scan | str] | None, replay_name: str | None) -> None:
        self.threshold = threshold
        self.tail = tail
        self.events: list[Scan | str] = events if events is not None else []
        self.k = len(self.events)       # cursor: render events[:k]
        self.k_rendered = -1
        self.playing = True
        self.speed = 1.0
        self._acc = 0.0                 # fractional events carried across ticks
        self._scrubbing = False         # guard: slider set by code, not a drag
        self.reboot_artists: list = []
        self._latest: Scan | None = None
        self.banner_msg: str | None = None
        self.alarms = 0

        plt.rcParams["font.family"] = "monospace"
        self.fig, self.ax = plt.subplots(figsize=(13, 4.8), facecolor=BG)
        self.fig.canvas.manager.set_window_title("MARS IDS -- live flash scan")
        replay = replay_name is not None
        self.fig.subplots_adjust(left=0.05, right=0.98, top=0.80,
                                 bottom=0.24 if replay else 0.16)
        ax = self.ax
        ax.set_facecolor(BG)
        for spine in ax.spines.values():
            spine.set_color(GRID)
            spine.set_alpha(0.25)
        ax.tick_params(colors=TRACE_DIM, labelsize=9)
        ax.grid(True, color=GRID, alpha=0.12, linewidth=0.6)
        ax.set_ylabel("anomaly score", color=TRACE_DIM, fontsize=10)
        ax.set_xlabel("scans -- the board runs one scan every 25 s",
                      color=TRACE_DIM, fontsize=9)

        ax.axhline(threshold, color=ALARM, linewidth=2.5)
        ax.text(0.995, threshold, f" ALARM LINE {threshold:.3f}",
                color=ALARM, fontsize=12, fontweight="bold",
                ha="right", va="bottom", transform=ax.get_yaxis_transform())

        (self.trace,) = ax.plot([], [], color=TRACE, linewidth=2.5, zorder=3)
        self.anom_dots = ax.scatter([], [], s=110, color=ALARM, zorder=4)
        self.head_dot = ax.scatter([], [], s=140, color=TRACE, zorder=5)

        self.fig.suptitle("MARS on-chip IDS -- the chip scanning its own flash, "
                          "every 25 seconds", color=TRACE_DIM, fontsize=13, y=0.97)
        # Counters live ABOVE the plot box so they can never collide with the
        # alarm-line label inside it.
        self.alarm_txt = ax.text(0.998, 1.03, "alarms: 0", transform=ax.transAxes,
                                 color=TRACE_DIM, fontsize=12, ha="right",
                                 va="bottom", zorder=6)
        self.score_txt = ax.text(0.012, 0.94, "--", transform=ax.transAxes,
                                 color=TRACE, fontsize=34, fontweight="bold",
                                 va="top", zorder=6)
        self.info_txt = ax.text(0.012, 0.56, "waiting for scan lines...",
                                transform=ax.transAxes, color=TRACE_DIM,
                                fontsize=11, va="top", zorder=6)
        # 0.72: below the big score text and the ~100-score dots of a planted
        # blob, so the money screenshot has no overlap (seen in rehearsal).
        self.banner = ax.text(0.5, 0.72, "", transform=ax.transAxes, color=ALARM,
                              fontsize=23, fontweight="bold", ha="center",
                              va="center", zorder=7)

        self.slider: Slider | None = None
        self.ctrl_txt = None
        if replay:
            ax.text(0.5, 0.06, f"REPLAY -- recorded log: {replay_name}",
                    transform=ax.transAxes, color=REPLAY_TAG, fontsize=12,
                    ha="center", va="bottom", zorder=6)
            self.ctrl_txt = ax.text(0.002, 1.03, "", transform=ax.transAxes,
                                    color=REPLAY_TAG, fontsize=10, va="bottom",
                                    zorder=6)
            ax_s = self.fig.add_axes([0.05, 0.045, 0.72, 0.035])
            ax_s.set_facecolor("#101010")
            self.slider = Slider(ax_s, "", 0, max(1, len(self.events)),
                                 valinit=self.k, valstep=1, color="#7a5400",
                                 initcolor="none")
            self.slider.valtext.set_color(TRACE_DIM)
            self.slider.on_changed(self._on_scrub)
            self.fig.canvas.mpl_connect("key_press_event", self._on_key)

    # ---- replay transport ---------------------------------------------------
    def _on_scrub(self, val: float) -> None:
        if self._scrubbing:
            return                       # our own programmatic update
        self.k = int(val)
        self.playing = False             # dragging the timeline means "hold it"

    def _on_key(self, event) -> None:
        if event.key == " ":
            self.playing = not self.playing
        elif event.key == "up":
            self.speed = min(SPEED_MAX, self.speed * 2)
        elif event.key == "down":
            self.speed = max(SPEED_MIN, self.speed / 2)
        elif event.key == "left":
            self.k = max(0, self.k - JUMP)
        elif event.key == "right":
            self.k = min(len(self.events), self.k + JUMP)
        elif event.key == "home":
            self.k = 0
        elif event.key == "end":
            self.k = len(self.events)

    # ---- state from events[:k] ----------------------------------------------
    def _recompute(self) -> tuple[list[int], list[float], list[bool], list[int]]:
        xs: list[int] = []
        ys: list[float] = []
        anom: list[bool] = []
        reboots: list[int] = []
        self.alarms = 0
        self.banner_msg = None
        self._latest = None
        for ev in self.events[:self.k]:
            if ev == "reboot":
                reboots.append(len(xs))  # scar sits before the next point
            elif ev == "hash":
                self.banner_msg = MSG_HASH
            elif ev == "dirty":
                self.alarms += 1
                self.banner_msg = MSG_DIRTY
            else:
                xs.append(len(xs))
                ys.append(ev.score)
                anom.append(ev.anomalous)
                self._latest = ev
                if ev.anomalous:
                    self.alarms += 1
                    self.banner_msg = MSG_ANOM
                else:
                    self.banner_msg = None   # a clean scan stands the board down
        return xs, ys, anom, reboots

    # ---- per-frame ------------------------------------------------------------
    def update(self, frame: int):
        if self.tail is not None:
            for line in self.tail.poll():
                ev = parse_event(line)
                if ev is not None:
                    self.events.append(ev)
            self.k = len(self.events)
        elif self.playing and self.k < len(self.events):
            self._acc += self.speed * REPLAY_EVENTS_PER_TICK
            step = int(self._acc)
            if step:
                self._acc -= step
                self.k = min(len(self.events), self.k + step)

        if self.slider is not None and int(self.slider.val) != self.k:
            self._scrubbing = True
            self.slider.set_val(self.k)
            self._scrubbing = False
        if self.ctrl_txt is not None:
            state = "playing" if self.playing else "PAUSED"
            self.ctrl_txt.set_text(f"SPACE {state}  UP/DOWN speed {self.speed:g}x"
                                   f"  LEFT/RIGHT jump  slider scrubs")

        if self.k != self.k_rendered:
            self.k_rendered = self.k
            xs, ys, anom, reboots = self._recompute()
            self._xs, self._ys, self._anom = xs, ys, anom

            lo = max(0, len(xs) - WINDOW)
            self.ax.set_xlim(lo - 0.5, max(len(xs), WINDOW) + 1.5)
            visible = ys[lo:] or [0.0]
            self.ax.set_ylim(0, max(16.0, self.threshold * 1.15,
                                    max(visible) * 1.12))

            self.trace.set_data(xs, ys)
            pts = [(x, y) for x, y, a in zip(xs, ys, anom) if a]
            self.anom_dots.set_offsets(pts if pts else [(float("nan"),) * 2])

            # Scrubbing can move BACKWARD, so scars are rebuilt, not appended.
            for artist in self.reboot_artists:
                artist.remove()
            self.reboot_artists = [
                self.ax.axvline(x - 0.5, color=ALARM, alpha=0.45, linewidth=1.4,
                                linestyle=(0, (2, 3))) for x in reboots]

            latest = self._latest
            if latest is not None:
                red = self._anom[-1]
                self.score_txt.set_text(f"{self._ys[-1]:.3f}")
                self.score_txt.set_color(ALARM if red else TRACE)
                slot = (f"slot {latest.slot}/{latest.slot_max}"
                        if latest.slot is not None else "slot --")
                seq = f"seq {latest.seq}" if latest.seq is not None else ""
                self.info_txt.set_text(f"{slot}  {seq}  scan #{latest.scan_no}")
            else:
                self.score_txt.set_text("--")
                self.score_txt.set_color(TRACE)
                self.info_txt.set_text("waiting for scan lines...")
            self.alarm_txt.set_text(f"alarms: {self.alarms}")
            self.alarm_txt.set_color(ALARM if self.alarms else TRACE_DIM)

        # The heartbeat + the blink run every frame, even while paused.
        if self._latest is not None:
            pulse = 140 + 70 * (0.5 + 0.5 * math.sin(frame * 0.35))
            self.head_dot.set_offsets([(self._xs[-1], self._ys[-1])])
            self.head_dot.set_sizes([pulse])
            self.head_dot.set_color(ALARM if self._anom[-1] else TRACE)
        show = self.banner_msg is not None and (frame % 8) < 5
        self.banner.set_text(self.banner_msg if show else "")
        return ()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Cardiac-monitor display of the board's live IDS scans.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--log", type=Path,
                     help="console_log.py output file to tail (the live mode)")
    src.add_argument("--replay", type=Path,
                     help="banked console/soak .txt to play back (watermarked, "
                          "with pause/speed/scrub controls)")
    args = ap.parse_args()

    threshold = load_threshold()
    if args.replay is not None:
        if not args.replay.exists():
            raise SystemExit(f"replay file not found: {args.replay}")
        text = args.replay.read_text(encoding="utf-8", errors="replace")
        events = [ev for line in text.splitlines()
                  if (ev := parse_event(line)) is not None]
        if not any(isinstance(ev, Scan) for ev in events):
            raise SystemExit(f"no [IDS] scan lines found in {args.replay}")
        meter = Meter(threshold, tail=None, events=events,
                      replay_name=args.replay.name)
        meter.k = 0                      # replay starts at the top
    else:
        if not args.log.exists():
            print(f"[meter] {args.log} does not exist yet -- waiting for "
                  f"console_log.py to create it")
        meter = Meter(threshold, tail=Tail(args.log), events=None,
                      replay_name=None)

    # Keep a reference or the animation is garbage-collected mid-show.
    anim = FuncAnimation(meter.fig, meter.update, interval=TICK_MS,
                         cache_frame_data=False)
    plt.show()
    del anim
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
