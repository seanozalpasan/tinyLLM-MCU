"""Prove the NS-flash static-vs-NV claim from two or more Bank-2 captures.

Slices each capture at NV_OFFSET into a CODE/.rodata region and an NV region, then checks:
  1. the CODE slice is byte-identical (equal SHA-256) across ALL captures;
  2. every byte that differs between any two captures lies at offset >= NV_OFFSET;
  3. the constant fraction of Bank 2 -- fixed by the page reservation, not the workload.
Exit code is non-zero if any claim fails, so this doubles as a pass/fail gate.

Usage: python analyze.py capture_0.bin capture_1.bin capture_2.bin [--nv-offset 0x3F000]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from itertools import combinations
from pathlib import Path

NV_OFFSET_DEFAULT = 0x3F000   # 0x0807F000 - 0x08040000: the top two 2 KB pages of Bank 2
BANK2_SIZE = 0x40000


def sha256(data: bytes) -> str:
    """Hex SHA-256 of a byte slice."""
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    """Run the three claims over the given captures; return 0 only if all pass."""
    ap = argparse.ArgumentParser(description="NS-flash static-vs-NV proof.")
    ap.add_argument("captures", nargs="+", type=Path, help="two or more Bank-2 .bin captures")
    ap.add_argument("--nv-offset", type=lambda s: int(s, 0), default=NV_OFFSET_DEFAULT,
                    help="NV region start offset within a Bank-2 dump (default 0x3F000)")
    args = ap.parse_args()

    if len(args.captures) < 2:
        ap.error("need at least two captures to diff")

    nv_off: int = args.nv_offset
    blobs: dict[str, bytes] = {}
    for path in args.captures:
        data = path.read_bytes()
        if len(data) != BANK2_SIZE:
            print(f"WARNING: {path.name} is {len(data)} bytes, expected 0x{BANK2_SIZE:X}")
        blobs[path.name] = data

    # ---- per-capture hashes ----
    print(f"NV_OFFSET = 0x{nv_off:05X} ({nv_off} bytes)   Bank-2 size = 0x{BANK2_SIZE:X}\n")
    code_hashes: dict[str, str] = {}
    for name, data in blobs.items():
        code, nv = data[:nv_off], data[nv_off:]
        code_hashes[name] = sha256(code)
        print(f"{name}")
        print(f"  CODE [0x00000:0x{nv_off:05X}]  sha256 {code_hashes[name]}")
        print(f"  NV   [0x{nv_off:05X}:0x{BANK2_SIZE:05X}]  sha256 {sha256(nv)}")
    print()

    ok = True

    # ---- claim 1: CODE identical across all captures ----
    if len(set(code_hashes.values())) == 1:
        print("PASS  CODE region byte-identical across all captures (firmware image immutable).")
    else:
        ok = False
        print("FAIL  CODE region DIFFERS across captures -- something wrote outside the NV region:")
        for name, digest in code_hashes.items():
            print(f"        {name}: {digest}")

    # ---- claim 2: every differing byte is inside the NV region ----
    for a, b in combinations(blobs, 2):
        da, db = blobs[a], blobs[b]
        diffs = [i for i in range(min(len(da), len(db))) if da[i] != db[i]]
        if not diffs:
            print(f"NOTE  {a} vs {b}: identical (no bytes changed).")
            continue
        lo, hi = diffs[0], diffs[-1]
        inside = lo >= nv_off
        ok = ok and inside
        tag = "PASS" if inside else "FAIL"
        print(f"{tag}  {a} vs {b}: {len(diffs)} bytes differ, "
              f"offsets 0x{lo:05X}..0x{hi:05X}, all inside NV region: {inside}")

    # ---- claim 3: constant fraction, set by the reservation ----
    pct = 100.0 * nv_off / BANK2_SIZE
    print(f"\nConstant region = 0x{nv_off:05X}/0x{BANK2_SIZE:05X} = {pct:.2f}% of Bank 2.")
    print("That fraction is fixed by the 2-page NV reservation, not by how many resets or setpoint changes were performed.")

    print("\n" + ("ALL CLAIMS PASS" if ok else "ONE OR MORE CLAIMS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
