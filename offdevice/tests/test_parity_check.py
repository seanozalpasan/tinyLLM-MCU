"""
Tests for the parity-gate checker (offdevice/features/parity_check.py).

The console builder here mirrors the firmware's [NVFPAR] print format
character-for-character (nv_features.c, NvFeatures_ParityPrint), so these
tests pin the whole laptop half of parity gate (a): bit-exact parsing through
console noise, PASS on the golden's own bits, tolerance-sized slack, and a
loud FAIL on anything larger.
"""

import numpy as np
import numpy.typing as npt
import pytest

from offdevice.features import params, parity_check
from offdevice.tests.make_golden import GOLDEN_PATH

needs_golden = pytest.mark.skipif(
    not GOLDEN_PATH.exists(),
    reason="golden missing; run `python -m offdevice.tests.make_golden` then commit the .npy",
)

N_DIMS = params.N_BINS * params.N_FEATURES


def golden_flat() -> npt.NDArray[np.float32]:
    """The golden as the chip emits it: feature-major (dataset.flatten_features order)."""
    golden = np.load(GOLDEN_PATH)
    return np.ascontiguousarray(golden.T).reshape(-1)


def make_console(vec: npt.NDArray[np.float32]) -> str:
    """Render a vector exactly as NvFeatures_ParityPrint does, plus boot noise."""
    bits = np.ascontiguousarray(vec).view(np.uint32)
    lines = [
        "[S ] Secure boot banner noise",
        "[HASH] OK 591523f3aaaaaaaa (hex-bearing noise; no [NVFPAR] -> ignored)",
        f"[NVFPAR] begin dims={N_DIMS}",
    ]
    for i in range(0, N_DIMS, 8):
        toks = "".join(f" {b:08X}" for b in bits[i:i + 8])
        lines.append(f"[NVFPAR] {i:3d}:{toks}")
    lines += ["[NVFPAR] end", "[S ] more noise after"]
    return "\r\n".join(lines)


@needs_golden
def test_roundtrip_parses_bitexact():
    vec = golden_flat()
    chip = parity_check.parse_console(make_console(vec))
    assert (chip.view(np.uint32) == vec.view(np.uint32)).all()


@needs_golden
def test_golden_bits_pass():
    vec = golden_flat()
    ok, lines = parity_check.evaluate(parity_check.parse_console(make_console(vec)),
                                      np.load(GOLDEN_PATH))
    assert ok
    assert all("PASS" in line for line in lines)


@needs_golden
def test_within_tolerance_passes():
    # A 0.05% nudge on the largest mel value sits inside the 0.1% gate.
    vec = golden_flat()
    idx = params.N_BINS + int(np.argmax(np.abs(vec[params.N_BINS:2 * params.N_BINS])))
    vec[idx] = np.float32(vec[idx] * (1.0 + 5e-4))
    ok, _ = parity_check.evaluate(vec, np.load(GOLDEN_PATH))
    assert ok


@needs_golden
def test_corrupted_value_fails_its_block_only():
    vec = golden_flat()
    idx = params.N_BINS + int(np.argmax(np.abs(vec[params.N_BINS:2 * params.N_BINS])))
    vec[idx] = np.float32(vec[idx] * 1.5)
    ok, lines = parity_check.evaluate(vec, np.load(GOLDEN_PATH))
    assert not ok
    verdicts = {name: ("FAIL" if "FAIL" in line else "PASS")
                for name, line in zip(params.FEATURE_ORDER, lines)}
    assert verdicts == {"mfcc": "PASS", "mel": "FAIL", "chroma_stft": "PASS"}


@needs_golden
def test_incomplete_console_rejected():
    vec = golden_flat()
    truncated = "\r\n".join(make_console(vec).splitlines()[:-3])
    with pytest.raises(ValueError, match="expected"):
        parity_check.parse_console(truncated)


def test_failed_extraction_rejected():
    with pytest.raises(ValueError, match="failed extraction"):
        parity_check.parse_console("[NVFPAR] begin dims=120\r\n"
                                   "[NVFPAR] EXTRACT FAILED\r\n[NVFPAR] end")
