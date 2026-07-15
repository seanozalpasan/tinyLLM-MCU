"""
Parity gate (a) checker: on-chip features vs the committed golden vector.

Feed it the console text of an NV_FEAT_PARITY=1 boot -- the [NVFPAR] lines
carry the chip's 120 outputs as raw IEEE-754 bit patterns, so the values are
rebuilt here bit-exactly (no decimal round-trip). Each feature block is then
compared against the committed golden at the tolerance agreed BEFORE any
measurement (rtol 1e-3 with per-feature atol floors); the verdict and the
measured worst-case gaps are printed, and the exit code is 0 only on PASS.

    python -m offdevice.features.parity_check <console.txt>
"""

import argparse
import re
from pathlib import Path

import numpy as np
import numpy.typing as npt

from offdevice.features import params
from offdevice.tests.make_golden import GOLDEN_PATH

# The agreed chip gate, fixed before measurement and held: float32 vs the
# golden's float64-ish reference should land ~1e-4 relative or better, so
# 0.1% keeps ~10x margin while any real porting bug (wrong window, off-by-one
# framing, a swapped table) misses by orders of magnitude. The atol floors
# only rescue near-zero elements, where a relative bar is meaningless.
CHIP_RTOL = 1e-3
CHIP_ATOL = {"mfcc": 1e-2, "mel": 1e-8, "chroma_stft": 1e-4}

MARKER = "[NVFPAR]"


def parse_console(text: str) -> npt.NDArray[np.float32]:
    """Rebuild the chip's 120 float32 values from pasted console text."""
    words: list[str] = []
    for line in text.splitlines():
        if MARKER not in line:
            continue
        if "FAILED" in line:
            raise ValueError(f"the chip reported a failed extraction: {line.strip()!r}")
        words += re.findall(r"\b[0-9A-Fa-f]{8}\b", line)
    n_dims = params.N_BINS * params.N_FEATURES
    if len(words) != n_dims:
        raise ValueError(
            f"expected {n_dims} hex words in {MARKER} lines, found {len(words)} -- "
            f"incomplete paste, or not an NV_FEAT_PARITY=1 boot")
    bits = np.array([int(w, 16) for w in words], dtype=np.uint32)
    return bits.view(np.float32)


def evaluate(chip: npt.NDArray[np.float32],
             golden: npt.NDArray[np.float32]) -> tuple[bool, list[str]]:
    """Per-feature-block verdicts + measured gaps; True only if every block passes."""
    if golden.shape != params.FEATURE_SHAPE:
        raise ValueError(f"golden shape {golden.shape} != {params.FEATURE_SHAPE}")
    if not np.isfinite(chip).all():
        return False, ["chip vector holds non-finite values -- broken extraction"]

    # The chip fills feature-major ([40 mfcc][40 mel][40 chroma]); the golden
    # is (40, 3) with the same column order -- one reshape lines them up.
    blocks = chip.reshape(params.N_FEATURES, params.N_BINS).astype(np.float64)
    lines: list[str] = []
    all_ok = True
    for j, name in enumerate(params.FEATURE_ORDER):
        gold = golden[:, j].astype(np.float64)
        diff = np.abs(blocks[j] - gold)
        allowed = CHIP_ATOL[name] + CHIP_RTOL * np.abs(gold)
        ok = bool((diff <= allowed).all())
        all_ok &= ok
        nonzero = gold != 0.0
        rel = float((diff[nonzero] / np.abs(gold[nonzero])).max()) if nonzero.any() else 0.0
        worst = int(np.argmax(diff - allowed))
        lines.append(
            f"{name:12s} {'PASS' if ok else 'FAIL'}  "
            f"max_abs={diff.max():.3e}  max_rel={rel:.3e}  "
            f"tightest_bin={worst} (chip={blocks[j][worst]:+.8e} golden={gold[worst]:+.8e})")
    return all_ok, lines


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare an NV_FEAT_PARITY=1 boot's console against the golden vector.")
    ap.add_argument("console", type=Path, help="text file holding the pasted boot console")
    args = ap.parse_args()

    try:
        chip = parse_console(args.console.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        print(f"[parity] {e}")
        return 1
    golden = np.load(GOLDEN_PATH)

    ok, lines = evaluate(chip, golden)
    print(f"[parity] gate: |chip - golden| <= atol + {CHIP_RTOL:g} * |golden|  "
          f"(atol: mfcc {CHIP_ATOL['mfcc']:g}, mel {CHIP_ATOL['mel']:g}, "
          f"chroma {CHIP_ATOL['chroma_stft']:g})")
    for line in lines:
        print(f"[parity] {line}")
    verdict = ("PARITY PASS: on-chip features match the golden vector" if ok
               else "PARITY FAIL")
    print(f"[parity] {verdict}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
