"""
Tests for the model parity-gate checker (offdevice/model/parity_check.py).

The console builder here mirrors the firmware's [MDLPAR] print format
character-for-character (nv_model_parity.c, NvModelParity_Print), so these
tests pin the whole laptop half of parity gate (b) before it ever grades real
hardware output: header reference parsing, bit-exact console parsing through
boot noise, PASS on the references' own float32 rounding, and loud FAILs on
corrupted scores, flipped verdicts, stale builds, and truncated pastes.
"""

import numpy as np
import pytest

from offdevice.model import parity_check

TESTVEC_PATH = parity_check.ENGINE_DIR / "nv_model_testvec.h"

needs_export = pytest.mark.skipif(
    not TESTVEC_PATH.exists(),
    reason="engine/nv_model_testvec.h missing; run offdevice.model.export first",
)
needs_firmware_copies = pytest.mark.skipif(
    not all((parity_check.FIRMWARE_INC / n).exists() for n in parity_check.MODEL_HEADERS),
    reason="firmware model-header copies missing; the on-chip port hasn't landed",
)


def header_refs() -> tuple[list[float], list[int], float, float, int]:
    return parity_check.parse_testvec_header(TESTVEC_PATH.read_text(encoding="ascii"))


def make_console(d2s: list[float], verdicts: list[int], dims: int,
                 count: int | None = None) -> str:
    """Render scores exactly as NvModelParity_Print does, plus boot noise."""
    lines = [
        "[S ] Secure boot banner noise",
        "[HASH] OK 591523f3 (hex-bearing noise; no [MDLPAR] -> ignored)",
        f"[MDLPAR] begin count={len(d2s) if count is None else count} "
        f"dims={dims} verdicts=1",
    ]
    for k, (d2, v) in enumerate(zip(d2s, verdicts)):
        bits = int(np.array([d2], dtype=np.float32).view(np.uint32)[0])
        lines.append(f"[MDLPAR] vec {k} d2={bits:08X} verdict={v}")
    lines += ["[MDLPAR] end", "[S ] more noise after"]
    return "\r\n".join(lines)


@needs_export
def test_reference_bits_pass():
    refs, verdicts, rel_tol, abs_tol, dims = header_refs()
    chip = parity_check.parse_console(make_console(refs, verdicts, dims),
                                      len(refs), dims)
    ok, lines = parity_check.evaluate(chip, refs, verdicts, rel_tol, abs_tol)
    assert ok
    assert all(line.endswith("ok") for line in lines)


@needs_export
def test_within_tolerance_passes():
    # Half the relative tolerance of drift on a nonzero reference still passes.
    refs, verdicts, rel_tol, abs_tol, dims = header_refs()
    d2s = list(refs)
    d2s[2] = refs[2] * (1.0 + rel_tol / 2)
    chip = parity_check.parse_console(make_console(d2s, verdicts, dims),
                                      len(refs), dims)
    ok, _ = parity_check.evaluate(chip, refs, verdicts, rel_tol, abs_tol)
    assert ok


@needs_export
def test_corrupted_score_fails_its_vector_only():
    refs, verdicts, rel_tol, abs_tol, dims = header_refs()
    d2s = list(refs)
    d2s[2] = refs[2] * 1.01   # 100x the relative tolerance
    chip = parity_check.parse_console(make_console(d2s, verdicts, dims),
                                      len(refs), dims)
    ok, lines = parity_check.evaluate(chip, refs, verdicts, rel_tol, abs_tol)
    assert not ok
    assert "FAIL(d2)" in lines[2]
    assert all(line.endswith("ok") for i, line in enumerate(lines) if i != 2)


@needs_export
def test_flipped_verdict_fails():
    refs, verdicts, rel_tol, abs_tol, dims = header_refs()
    flipped = list(verdicts)
    flipped[5] ^= 1
    chip = parity_check.parse_console(make_console(refs, flipped, dims),
                                      len(refs), dims)
    ok, lines = parity_check.evaluate(chip, refs, verdicts, rel_tol, abs_tol)
    assert not ok
    assert "FAIL(verdict)" in lines[5]


@needs_export
def test_truncated_console_rejected():
    refs, verdicts, _, _, dims = header_refs()
    truncated = "\r\n".join(make_console(refs, verdicts, dims).splitlines()[:-3])
    with pytest.raises(ValueError, match="truncated|cover"):
        parity_check.parse_console(truncated, len(refs), dims)


@needs_export
def test_console_without_begin_rejected():
    refs, verdicts, _, _, dims = header_refs()
    with pytest.raises(ValueError, match="begin"):
        parity_check.parse_console("[S ] boot noise only, no marker lines",
                                   len(refs), dims)


@needs_export
def test_stale_build_rejected():
    # A chip announcing a different vector count / dims than the header ships.
    refs, verdicts, _, _, dims = header_refs()
    stale = make_console(refs, verdicts, dims, count=len(refs) - 1)
    with pytest.raises(ValueError, match="stale build"):
        parity_check.parse_console(stale, len(refs), dims)


@needs_export
def test_chip_refusal_rejected():
    refs, _, _, _, dims = header_refs()
    console = ("[MDLPAR] begin count=9 dims=120 verdicts=1\r\n"
               "[MDLPAR] DIMS MISMATCH scorer=120 testvec=119\r\n"
               "[MDLPAR] end")
    with pytest.raises(ValueError, match="refused"):
        parity_check.parse_console(console, len(refs), dims)


@needs_export
def test_verdictless_vec_line_rejected():
    refs, verdicts, _, _, dims = header_refs()
    console = make_console(refs, verdicts, dims).replace(" verdict=0", "", 1)
    with pytest.raises(ValueError, match="plumbing"):
        parity_check.parse_console(console, len(refs), dims)


@needs_export
@needs_firmware_copies
def test_engine_and_firmware_headers_identical():
    # The staleness guard itself: passes only while the firmware copies are
    # byte-identical to engine/'s -- the same check the gate runs before grading.
    parity_check.check_header_copies()
