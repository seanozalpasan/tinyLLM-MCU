"""Feature-extraction tests: scaling, window contract, shape, determinism, golden.

Run from the repo root:
    pytest offdevice/tests/test_features.py -v

The golden test FAILS (not skips) against a stale golden: after any deliberate
re-tune, regenerate first:
    python -m offdevice.tests.make_golden
then commit offdevice/tests/golden/synthetic_features.npy.
"""

from pathlib import Path

import numpy as np
import pytest

from offdevice.features import extract, params
from offdevice.nv import parse, spec
from offdevice.tests import fixtures
from offdevice.tests.fixtures import synthetic_nv_region

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


def test_window_ties_to_nv_spec():
    # params.py states 4096 independently; if the NV region ever resized, this
    # is the loud reminder that the feature contract must be re-tuned with it.
    assert params.WINDOW_BYTES == spec.REGION_SIZE


def test_rejects_non_window_input():
    # A whole 256 KB capture (or any other size) is a caller bug, not a window.
    with pytest.raises(ValueError):
        extract.extract_features(bytes(100))
    with pytest.raises(ValueError):
        extract.extract_features(bytes(262144))


def test_fixture_is_spec_conformant():
    # The golden input must parse as a valid ring, or the reference numbers
    # characterize a byte regime the model never sees.
    view = parse.parse_region(synthetic_nv_region())
    assert view.current == 1
    recs = parse.records_chronological(view)
    assert len(recs) == spec.RECORDS_PER_PAGE + fixtures.PAGE1_RECORDS
    ts = [r["ts"] for r in recs]
    assert ts == sorted(ts)


def test_shape_and_dtype():
    feats = extract.extract_features(synthetic_nv_region())
    assert feats.shape == params.FEATURE_SHAPE   # (40, 3)
    assert feats.dtype == params.SIGNAL_DTYPE     # float32


def test_features_finite_and_nonflat():
    # NaN/inf would poison the model fit silently; an all-constant column means a
    # degenerate feature (e.g. empty mel filters) that re-tuning must have avoided.
    feats = extract.extract_features(synthetic_nv_region())
    assert np.isfinite(feats).all()
    for j in range(params.N_FEATURES):
        assert feats[:, j].std() > 0.0


def test_determinism():
    # Same input -> byte-identical (40, 3) on every run.
    a = extract.extract_features(synthetic_nv_region())
    b = extract.extract_features(synthetic_nv_region())
    assert np.array_equal(a, b)


@pytest.mark.skipif(
    not GOLDEN_PATH.exists(),
    reason="golden missing; run `python -m offdevice.tests.make_golden` then commit the .npy",
)
def test_golden_regression():
    golden = np.load(GOLDEN_PATH)
    feats = extract.extract_features(synthetic_nv_region())
    assert feats.shape == golden.shape
    # Tight tolerance: catches real feature-logic changes while tolerating
    # negligible FP noise from a librosa/numpy patch bump. A deliberate feature
    # change means re-running make_golden to re-freeze.
    np.testing.assert_allclose(feats, golden, rtol=1e-5, atol=1e-6)
