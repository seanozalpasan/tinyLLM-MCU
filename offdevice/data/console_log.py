"""
Console soak logger: tee the board's secure console to a timestamped .txt.

Sits beside capture.py in the toolchain but is strictly read-only: it opens the
ST-LINK VCP, decodes console lines, prefixes each with elapsed time, and writes
them to a file flushed line-by-line -- so a long soak's full history survives
(a terminal scrollback cap once ate the first hour of a watchdog soak; this
exists so that never happens again). It never sends a byte to the board.

GOTCHA: the ST-LINK VCP can pulse DTR/RTS when the port opens and reset the
target (same as capture.py) -- start the logger BEFORE the stretch you care
about, never in the middle of it. Stop with Ctrl+C (or let --minutes expire);
the port must be free (this logger AND CoolTerm closed) before collect.py or
capture.py can run.

    python -m offdevice.data.console_log --minutes 75 --out proofStep6Run.txt
"""

from __future__ import annotations

import argparse
import re
import time
from datetime import datetime
from pathlib import Path

import serial  # pyserial

DEFAULT_PORT = "COM3"
BAUD = 921600          # the board's one static console config (8N1)
READ_TIMEOUT_S = 1.0   # bounds how often the deadline and Ctrl+C get a look-in

_SCORE_RE = re.compile(r"score=(\d+\.\d+)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Tee the board's console to a timestamped file (read-only).")
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--minutes", type=float, default=75.0,
                    help="stop after this long (default 75; Ctrl+C also stops)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output .txt (default console_soak_<timestamp>.txt)")
    args = ap.parse_args()
    out = args.out or Path(f"console_soak_{datetime.now():%Y%m%dT%H%M%S}.txt")

    n_lines = 0
    n_anom = 0
    max_score: float | None = None
    start = time.monotonic()
    deadline = start + args.minutes * 60.0
    try:
        with serial.Serial(args.port, BAUD, timeout=READ_TIMEOUT_S,
                           dsrdtr=False, rtscts=False) as port, \
             out.open("w", encoding="utf-8") as f:
            print(f"[log] {args.port} @ {BAUD} -> {out} for {args.minutes:g} min "
                  f"(Ctrl+C to stop)")
            while time.monotonic() < deadline:
                raw = port.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="replace").rstrip("\r\n")
                elapsed = int(time.monotonic() - start)
                stamp = f"[+{elapsed // 60:02d}:{elapsed % 60:02d}]"
                f.write(f"{stamp} {line}\n")
                f.flush()
                print(f"{stamp} {line}")
                n_lines += 1
                if "ANOMALY" in line:
                    n_anom += 1
                m = _SCORE_RE.search(line)
                if m:
                    s = float(m.group(1))
                    max_score = s if max_score is None else max(max_score, s)
    except KeyboardInterrupt:
        pass
    except serial.SerialException as e:
        print(f"[log] serial error: {e} (is CoolTerm still holding {args.port}?)")
        return 1
    top = "n/a" if max_score is None else f"{max_score:.3f}"
    print(f"\n[log] {n_lines} lines -> {out}; ANOMALY lines: {n_anom}; "
          f"max score seen: {top}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
