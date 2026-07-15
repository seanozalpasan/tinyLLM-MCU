"""
Tier-1 gate for the on-chip feature port (run from the repo root):

    pytest offdevice/tests/test_feat_tables.py -v

Proves, before any C exists: (1) each frozen table is exactly what librosa
uses internally under params.py; (2) the table-only reference pipeline
(tables.extract_features_ref -- the C port's blueprint) reproduces librosa's
output, including on edge inputs that exercise the guard paths; (3) the
generated headers are current and their literals round-trip bit-for-bit.
If a generated-header test fails: python -m offdevice.features.gen_tables
"""

import re

import numpy as np
import pytest
import librosa
import scipy.fftpack

from offdevice.features import extract, gen_tables, params, tables
from offdevice.tests.fixtures import synthetic_nv_region
from offdevice.tests.make_golden import GOLDEN_PATH

# Reference-vs-librosa gates: 10x tighter than the chip's agreed parity gate
# (rtol 1e-3), so the decomposition proof never leans on the chip's slack.
# atol floors sit well under each block's smallest benign magnitude and only
# rescue elements near zero, where a relative bar is meaningless.
REF_RTOL = 1e-4
REF_ATOL = {"mfcc": 1e-3, "mel": 1e-9, "chroma_stft": 1e-5}

_rng = np.random.default_rng(20260714)
RANDOM_BYTES = _rng.integers(0, 256, params.WINDOW_BYTES, dtype=np.uint8).tobytes()


def _assert_ref_matches(ref, lib):
    """Per-feature comparison -- one shared atol would drown the tiny mel values."""
    assert ref.shape == params.FEATURE_SHAPE
    assert ref.dtype == np.float32
    for j, name in enumerate(params.FEATURE_ORDER):
        np.testing.assert_allclose(ref[:, j], lib[:, j], rtol=REF_RTOL,
                                   atol=REF_ATOL[name],
                                   err_msg=f"{name} column diverged from librosa")


# ---- the tables themselves --------------------------------------------------


def test_shared_bank_precondition():
    # The whole single-bank port design stands on this params identity.
    assert params.MFCC_INTERNAL_N_MELS == params.N_MELS
    assert params.MFCC_FMAX == params.FMAX


def test_hann_window_matches_closed_form():
    win = tables.hann_window()
    assert win.shape == (params.N_FFT,)
    assert win.dtype == np.float32
    n = np.arange(params.N_FFT)
    closed = (0.5 - 0.5 * np.cos(2 * np.pi * n / params.N_FFT)).astype(np.float32)
    np.testing.assert_allclose(win, closed, rtol=0, atol=1e-7)
    assert win[0] == 0.0   # periodic (fftbins=True), not symmetric


def test_mel_bank_shape_and_no_empty_filters():
    bank = tables.mel_filterbank()
    assert bank.shape == (params.N_MELS, tables.N_SPEC_BINS)
    assert bank.dtype == np.float32
    assert np.isfinite(bank).all()
    assert (bank >= 0).all()
    # An empty filter is a constant feature row = a zero-variance dimension
    # that breaks the Mahalanobis covariance -- the n_fft=512 floor exists
    # exactly to prevent this.
    assert (bank.sum(axis=1) > 0).all()


def test_one_bank_serves_mfcc_and_mel():
    # Bit-identical to the bank librosa.feature.mfcc builds internally from
    # the pinned MFCC params -- the engineered identity the port relies on.
    mfcc_bank = librosa.filters.mel(sr=params.SR, n_fft=params.N_FFT,
                                    n_mels=params.MFCC_INTERNAL_N_MELS,
                                    fmax=params.MFCC_FMAX)
    assert (mfcc_bank == tables.mel_filterbank()).all()


def test_dct_matrix_is_scipys_transform():
    rng = np.random.default_rng(28278)
    x = rng.standard_normal((params.N_MELS, tables.N_FRAMES))
    want = scipy.fftpack.dct(x, axis=-2, type=2, norm="ortho")[: params.N_MFCC, :]
    got = tables.dct_matrix().astype(np.float64) @ x
    # atol sized to the float32 rounding of the matrix entries: a near-zero
    # output element still carries the absolute error of its O(1)-magnitude
    # inputs (~1e-7), where a relative bar is meaningless. A real defect (a
    # wrong norm, a transposed matrix) misses by ~40%, four decades above this.
    np.testing.assert_allclose(got, want, rtol=1e-5, atol=1e-5)


def test_chroma_bank_shape_and_health():
    bank = tables.chroma_filterbank()
    assert bank.shape == (params.N_CHROMA, tables.N_SPEC_BINS)
    assert bank.dtype == np.float32
    assert np.isfinite(bank).all()
    assert (bank >= 0).all()
    assert (bank.sum(axis=1) > 0).all()


# ---- the decomposition proof: tables + primitive ops == librosa --------------


def test_reference_matches_librosa_on_fixture():
    raw = synthetic_nv_region()
    _assert_ref_matches(tables.extract_features_ref(raw),
                        extract.extract_features(raw))


@pytest.mark.parametrize("raw", [
    pytest.param(RANDOM_BYTES, id="random"),
    pytest.param(b"\xff" * params.WINDOW_BYTES, id="erased-ff"),
    # All-zero input walks the guard paths: the AMIN floor carries the whole
    # dB matrix and every chroma frame peak sits below NORM_TINY.
    pytest.param(b"\x00" * params.WINDOW_BYTES, id="zeros"),
])
def test_reference_matches_librosa_on_edge_inputs(raw):
    _assert_ref_matches(tables.extract_features_ref(raw),
                        extract.extract_features(raw))


@pytest.mark.skipif(
    not GOLDEN_PATH.exists(),
    reason="golden missing; run `python -m offdevice.tests.make_golden` then commit the .npy",
)
def test_reference_reproduces_committed_golden():
    # The golden is what Wednesday's on-chip run is checked against, so the
    # blueprint must reproduce it, not just live librosa.
    golden = np.load(GOLDEN_PATH)
    _assert_ref_matches(tables.extract_features_ref(synthetic_nv_region()), golden)


def test_reference_rejects_wrong_window_size():
    with pytest.raises(ValueError):
        tables.extract_features_ref(b"\x00" * (params.WINDOW_BYTES - 1))


# ---- the generated headers ---------------------------------------------------


def test_generated_headers_current():
    for target, text in ((gen_tables.TABLES_TARGET, gen_tables.render_tables_header()),
                         (gen_tables.FIXTURE_TARGET, gen_tables.render_fixture_header())):
        assert target.exists(), \
            f"missing {target.name} -- run `python -m offdevice.features.gen_tables`"
        assert target.read_text(encoding="ascii") == text, \
            f"{target.name} drifted -- run `python -m offdevice.features.gen_tables`"


def test_c_float_roundtrips_every_table_value():
    # 9-significant-digit literals must parse back to the identical float32
    # bits -- the tables the firmware compiles ARE the tables librosa used.
    vals = np.concatenate([tables.hann_window(),
                           tables.mel_filterbank().ravel(),
                           tables.dct_matrix().ravel(),
                           tables.chroma_filterbank().ravel()])
    parsed = np.array([float(gen_tables._c_float(v)[:-1]) for v in vals],
                      dtype=np.float32)
    assert (parsed.view(np.uint32) == vals.view(np.uint32)).all()


def test_fixture_header_roundtrips_bytes():
    body = gen_tables.render_fixture_header().split("{", 1)[1]
    data = bytes(int(m, 16) for m in re.findall(r"0x([0-9A-Fa-f]{2})", body))
    assert data == synthetic_nv_region()
