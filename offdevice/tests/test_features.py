"""Feature-extraction tests: scaling, shape, dtype, determinism, golden regression.

Run from the repo root:
    pytest offdevice/tests/test_features.py -v

The golden test is SKIPPED until you generate the golden vector once:
    python -m offdevice.tests.make_golden
then commit offdevice/tests/golden/synthetic_features.npy.
"""

from pathlib import Path

import numpy as np
import pytest

from offdevice.features import extract, params
from offdevice.tests.fixtures import synthetic_dump

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "synthetic_features.npy"


def test_signal_scaling_range():
    # byte 0..255 widened to int16 then /32768 -> [0, 255/32768], all positive.
    y = extract.bytes_to_signal(bytes([0, 255, 128]))
    assert y.dtype == params.SIGNAL_DTYPE
    assert y.min() >= 0.0
    assert y.max() <= 255.0 / 32768.0 + 1e-9
    assert np.isclose(y[0], 0.0)
    assert np.isclose(y[1], 255.0 / 32768.0)
    assert np.isclose(y[2], 128.0 / 32768.0)


def test_shape_and_dtype():
    feats = extract.extract_features(synthetic_dump())
    assert feats.shape == params.FEATURE_SHAPE   # (40, 3)
    assert feats.dtype == params.SIGNAL_DTYPE     # float32


def test_determinism():
    # Same input -> byte-identical (40, 3) on every run.
    a = extract.extract_features(synthetic_dump())
    b = extract.extract_features(synthetic_dump())
    assert np.array_equal(a, b)


@pytest.mark.skipif(
    not GOLDEN_PATH.exists(),
    reason="golden missing; run `python -m offdevice.tests.make_golden` then commit the .npy",
)
def test_golden_regression():
    golden = np.load(GOLDEN_PATH)
    feats = extract.extract_features(synthetic_dump())
    assert feats.shape == golden.shape
    # Tight tolerance: catches real feature-logic changes while tolerating
    # negligible FP noise from a librosa/numpy patch bump. A deliberate feature
    # change means re-running make_golden to re-freeze.
    np.testing.assert_allclose(feats, golden, rtol=1e-5, atol=1e-6)
