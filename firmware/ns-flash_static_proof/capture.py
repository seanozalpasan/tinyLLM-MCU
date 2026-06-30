"""Emit the exact STM32CubeProgrammer command to dump Bank 2 (NS flash) to a .bin.

This prints the command for you to paste; it does NOT run anything, so the capture step
stays under your control. HOTPLUG connect is deliberate: connecting under reset would
reboot the board and add a phantom increment to the boot-counter NV page.
"""
from __future__ import annotations

import argparse

BANK2_BASE = 0x08040000   # non-secure bank base
BANK2_SIZE = 0x40000      # 256 KB, the whole bank (CODE/.rodata + NV region)


def main() -> None:
    ap = argparse.ArgumentParser(description="Print the Bank-2 capture command (does not run it).")
    ap.add_argument("outfile", help="output .bin path, e.g. capture_0.bin")
    args = ap.parse_args()

    cmd = (
        f"STM32_Programmer_CLI -c port=SWD mode=HOTPLUG "
        f"-u 0x{BANK2_BASE:08X} 0x{BANK2_SIZE:X} {args.outfile}"
    )
    print("# Dump Bank 2 without resetting the board (no phantom boot):")
    print(cmd)


if __name__ == "__main__":
    main()
