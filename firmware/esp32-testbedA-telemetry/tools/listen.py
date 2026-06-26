"""Test-bed A laptop listener -- the preferred receiver for the STM32 -> ESP32 -> Wi-Fi
telemetry stream. `ncat -l -k 9000` also works, but this is preferred: it never exits when
a client disconnects (it re-accepts forever, where ncat -k can drop on Windows), timestamps
every line, and flags any break in the telemetry seq counter -- so a soak's "no gaps == not
theater" check is automatic.

Run (from anywhere; stdlib only):
    python listen.py [port]        # default 9000
When Windows prompts to allow python on the network, choose Allow (private networks).
"""

from __future__ import annotations

import re
import socket
import sys
from datetime import datetime

SEQ_RE = re.compile(rb"seq=(\d+)")


def serve(port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))      # all interfaces, so the hotspot subnet reaches it
    srv.listen(1)
    print(f"[listen] TCP :{port} -- waiting for the ESP32 (Ctrl-C to stop)")

    last_seq: int | None = None
    while True:                      # re-accept forever; a client drop never ends us
        conn, addr = srv.accept()
        print(f"[listen] connected from {addr[0]}:{addr[1]}")
        buf = b""
        with conn:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    print("[listen] client disconnected -- waiting for next")
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    note = ""
                    match = SEQ_RE.search(raw)
                    if match:
                        seq = int(match.group(1))
                        if last_seq is not None and seq != last_seq + 1:
                            note = f"  <-- GAP (expected {last_seq + 1}, got {seq})"
                        last_seq = seq
                    stamp = datetime.now().strftime("%H:%M:%S")
                    print(f"{stamp}  {raw.decode('ascii', 'replace').rstrip()}{note}")


if __name__ == "__main__":
    try:
        serve(int(sys.argv[1]) if len(sys.argv) > 1 else 9000)
    except KeyboardInterrupt:
        print("\n[listen] stopped")
