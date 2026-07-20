"""
Parity gate (b) checker: on-chip Mahalanobis scores vs the exported references.

Feed it the console text of an NV_MODEL_PARITY=1 boot -- the [MDLPAR] lines
carry the chip's squared distance for each exported test vector as a raw
IEEE-754 bit pattern, rebuilt here bit-exactly (no decimal round-trip). Each
is compared against nv_model_testvec.h's float64 reference at the header's
own baked-in tolerances, and the chip's verdicts must match the header's
exactly -- the 0.99x/1.01x threshold-bracketing vectors prove the alarm line
sits where the laptop put it. Before any grading, the engine/ headers are
byte-compared against the firmware copies, so a stale copy fails loudly
instead of silently grading the chip against the wrong model.

    python -m offdevice.model.parity_check <console.txt>
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
FIRMWARE_INC = REPO_ROOT / "firmware" / "memAcq" / "Secure" / "Core" / "Inc"
# Both generated headers must match: the chip compiled the firmware copies,
# the references below come from the engine copy.
MODEL_HEADERS = ("nv_model_params.h", "nv_model_testvec.h")

MARKER = "[MDLPAR]"

_BEGIN_RE = re.compile(r"begin count=(\d+) dims=(\d+) verdicts=(\d+)")
_VEC_RE = re.compile(r"vec (\d+) d2=([0-9A-Fa-f]{8})(?: verdict=([01]))?")
_END_RE = re.compile(r"\[MDLPAR\]\s+end\b")
_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def check_header_copies() -> None:
    """Refuse to grade if engine/ and firmware model headers differ."""
    for name in MODEL_HEADERS:
        engine, firmware = ENGINE_DIR / name, FIRMWARE_INC / name
        if engine.read_bytes() != firmware.read_bytes():
            raise ValueError(
                f"{name} differs between engine/ and the firmware -- re-copy the "
                f"engine headers (or re-export) before trusting any parity verdict")


def _header_int(text: str, macro: str) -> int:
    m = re.search(rf"#define {macro}\s+(\d+)U?\b", text)
    if m is None:
        raise ValueError(f"{macro} not found in nv_model_testvec.h -- not an export?")
    return int(m.group(1))


def _header_float(text: str, macro: str) -> float:
    m = re.search(rf"#define {macro}\s+({_NUM_RE.pattern})", text)
    if m is None:
        raise ValueError(f"{macro} not found in nv_model_testvec.h -- not an export?")
    return float(m.group(1))


def _header_array_block(text: str, array_name: str) -> str:
    m = re.search(rf"{array_name}\[[^\]]*\]\s*=\s*\{{(.*?)\}};", text, re.DOTALL)
    if m is None:
        raise ValueError(f"array {array_name} not found in nv_model_testvec.h")
    return m.group(1)


def parse_testvec_header(text: str) -> tuple[list[float], list[int], float, float, int]:
    """The export's references: (d2_ref, verdicts, rel_tol, abs_tol, dims)."""
    count = _header_int(text, "NV_MODEL_TESTVEC_COUNT")
    dims = _header_int(text, "NV_MODEL_TESTVEC_DIMS")
    if _header_int(text, "NV_MODEL_TESTVEC_HAS_VERDICTS") != 1:
        raise ValueError(
            "nv_model_testvec.h carries no verdicts -- a plumbing export is on the "
            "chip; gate (b) must run against the real model")
    rel_tol = _header_float(text, "NV_MODEL_TESTVEC_REL_TOL")
    abs_tol = _header_float(text, "NV_MODEL_TESTVEC_ABS_TOL")
    refs = [float(t) for t in _NUM_RE.findall(
        _header_array_block(text, "nv_model_testvec_d2_ref"))]
    verdicts = [int(t) for t in _NUM_RE.findall(
        _header_array_block(text, "nv_model_testvec_verdict"))]
    if len(refs) != count or len(verdicts) != count:
        raise ValueError(f"header arrays hold {len(refs)} refs / {len(verdicts)} "
                         f"verdicts but COUNT is {count} -- corrupt header?")
    return refs, verdicts, rel_tol, abs_tol, dims


def parse_console(text: str, expect_count: int,
                  expect_dims: int) -> list[tuple[float, int]]:
    """The chip's (d2, verdict) per vector, rebuilt bit-exactly from [MDLPAR] lines."""
    begin: tuple[int, int, int] | None = None
    seen: dict[int, tuple[float, int]] = {}
    ended = False
    for line in text.splitlines():
        if MARKER not in line:
            continue
        if "MISMATCH" in line:
            raise ValueError(f"the chip refused to score: {line.strip()!r}")
        if (m := _BEGIN_RE.search(line)) is not None:
            begin = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            seen.clear()          # a later boot in the same paste supersedes
            ended = False
        elif (m := _VEC_RE.search(line)) is not None:
            if m.group(3) is None:
                raise ValueError(f"vec line without a verdict: {line.strip()!r} -- "
                                 f"a plumbing build on the chip?")
            bits = np.array([int(m.group(2), 16)], dtype=np.uint32)
            d2 = float(bits.view(np.float32)[0])
            seen[int(m.group(1))] = (d2, int(m.group(3)))
        elif _END_RE.search(line) is not None:
            ended = True
    if begin is None:
        raise ValueError(f"no '{MARKER} begin' line found -- not an "
                         f"NV_MODEL_PARITY=1 boot, or an incomplete paste")
    if begin[0] != expect_count or begin[1] != expect_dims:
        raise ValueError(f"chip ran {begin[0]} vectors of {begin[1]} dims, header "
                         f"ships {expect_count} of {expect_dims} -- stale build "
                         f"on the chip?")
    if not ended:
        raise ValueError("the [MDLPAR] block never ended -- truncated paste?")
    if sorted(seen) != list(range(expect_count)):
        raise ValueError(f"vec lines cover {sorted(seen)} -- expected "
                         f"0..{expect_count - 1} exactly once each")
    return [seen[k] for k in range(expect_count)]


def evaluate(chip: list[tuple[float, int]], refs: list[float], verdicts: list[int],
             rel_tol: float, abs_tol: float) -> tuple[bool, list[str]]:
    """Per-vector verdicts, mirroring engine/parity_main.c's PASS criterion."""
    lines: list[str] = []
    all_ok = True
    for k, ((d2, verdict), ref, want) in enumerate(zip(chip, refs, verdicts)):
        err = abs(d2 - ref)
        lim = rel_tol * abs(ref) + abs_tol
        d2_ok = bool(np.isfinite(d2)) and err <= lim
        v_ok = verdict == want
        all_ok &= d2_ok and v_ok
        status = "ok" if (d2_ok and v_ok) else ("FAIL(verdict)" if d2_ok else "FAIL(d2)")
        lines.append(f"vec {k}  d2={d2:<15.9g} ref={ref:<15.9g} err={err:<10.3g} "
                     f"{'ANOMALY' if verdict else 'benign':7s} {status}")
    return all_ok, lines


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare an NV_MODEL_PARITY=1 boot's console against the "
                    "exported model references (parity gate (b)).")
    ap.add_argument("console", type=Path, help="text file holding the pasted boot console")
    args = ap.parse_args()

    try:
        check_header_copies()
        refs, verdicts, rel_tol, abs_tol, dims = parse_testvec_header(
            (ENGINE_DIR / "nv_model_testvec.h").read_text(encoding="ascii"))
        chip = parse_console(
            args.console.read_text(encoding="utf-8", errors="replace"),
            len(refs), dims)
    except (OSError, ValueError) as e:
        print(f"[parity] {e}")
        return 1

    ok, lines = evaluate(chip, refs, verdicts, rel_tol, abs_tol)
    print(f"[parity] gate: |chip - ref| <= {rel_tol:g} * |ref| + {abs_tol:g}, "
          f"and every verdict exact")
    for line in lines:
        print(f"[parity] {line}")
    verdict = ("MODEL PARITY PASS: the chip computes the exported model"
               if ok else "MODEL PARITY FAIL")
    print(f"[parity] {verdict}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
