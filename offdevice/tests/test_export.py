"""
Unit tests for the model export path: C literal round-tripping, the float32 host
mirror vs the float64 reference, test-vector construction, the export gates, and
(when a host gcc exists) an end-to-end compile+run of the engine/ parity binary
against a fabricated model.

Run from the repo root (so `import offdevice...` resolves):
    pytest offdevice\\tests\\test_export.py -v
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from offdevice.model.dataset import N_DIMS
from offdevice.model.export import (
    DEFAULT_OUT_DIR,
    TESTVEC_REL_TOL,
    THRESHOLD_FACTORS,
    _c_float,
    export_artifact,
    make_test_vectors,
    score_d2_f32,
)
from offdevice.model.fit import MahalanobisModel, save_model

FLOAT_TOKEN = re.compile(r"[-+]?[0-9][0-9.eE+-]*f")
DOUBLE_TOKEN = re.compile(r"[-+]?[0-9][0-9.eE+-]*")


# ---- fabricated models (tests never touch a real fit) -----------------------------

def small_model(dims: int = 6, threshold: float | None = 3.0,
                holdout: str | None = "offdevice/data/holdout.txt") -> MahalanobisModel:
    """A well-conditioned symmetric-positive-definite toy model."""
    rng = np.random.default_rng(7)
    a = rng.standard_normal((dims, dims))
    precision = a @ a.T + dims * np.eye(dims)
    precision = (precision + precision.T) / 2.0
    mean = rng.standard_normal(dims) * 5.0
    meta = {"holdout_file": holdout, "n_train": 12, "fp_target": 0.05,
            "feature_pins": {"n_fft": 512}, "nv_spec_version": 1}
    return MahalanobisModel(mean=mean, precision=precision, threshold=threshold,
                            meta=meta)


def saved(tmp_path: Path, model: MahalanobisModel) -> Path:
    npz, _ = save_model(model, tmp_path / "toy")
    return npz


def block(text: str, name: str) -> str:
    m = re.search(name + r"[^=]*=\n\{\n(.*?)\n\};", text, re.S)
    assert m, f"array {name} not found in generated header"
    return m.group(1)


def has_define(text: str, name: str, value: str) -> bool:
    """True if the header #defines name to value (whitespace-tolerant on purpose:
    the generator's column alignment is cosmetic, not part of the contract)."""
    return re.search(rf"#define {name}\s+{re.escape(value)}\b", text) is not None


# ---- C literals --------------------------------------------------------------------

def test_c_float_roundtrips_exactly() -> None:
    rng = np.random.default_rng(1)
    scales = 10.0 ** rng.integers(-6, 7, size=50)
    values = np.concatenate([rng.standard_normal(50) * scales,
                             [0.0, 1.0, -2.0, 100.0, 1e12, -0.0]])
    for v in values:
        lit = _c_float(v)
        assert lit.endswith("f")
        assert "." in lit or "e" in lit   # "1f" would not be a C float literal
        assert np.float32(float(lit[:-1])) == np.float32(v)


def test_c_float_rejects_nonfinite() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        _c_float(np.inf)


# ---- mirror vs float64 reference ----------------------------------------------------

def test_mirror_matches_float64_within_tolerance() -> None:
    # Full 120 dims: this measured gap is the evidence behind TESTVEC_REL_TOL.
    model = small_model(dims=N_DIMS)
    mean32 = model.mean.astype(np.float32)
    prec32 = model.precision.astype(np.float32)
    xs, refs = make_test_vectors(mean32, prec32, model.threshold)
    for x, ref in zip(xs, refs):
        assert score_d2_f32(mean32, prec32, x) == pytest.approx(
            ref, rel=TESTVEC_REL_TOL, abs=1e-9)


def test_test_vectors_walk_and_bracket_the_threshold() -> None:
    model = small_model(dims=6, threshold=3.0)
    mean32 = model.mean.astype(np.float32)
    prec32 = model.precision.astype(np.float32)
    xs, refs = make_test_vectors(mean32, prec32, model.threshold)
    assert xs.shape == (len(THRESHOLD_FACTORS), 6)
    assert refs[0] == 0.0   # the self-vector: diff is exactly zero
    # Each reference distance lands on its target factor (float32 rounding of the
    # vector perturbs it only in the far decimals).
    for factor, ref in zip(THRESHOLD_FACTORS, refs):
        assert math.sqrt(ref) == pytest.approx(factor * 3.0, rel=1e-3, abs=1e-9)
    # Determinism: the vectors are part of the parity evidence.
    xs2, refs2 = make_test_vectors(mean32, prec32, model.threshold)
    assert np.array_equal(xs, xs2) and np.array_equal(refs, refs2)


# ---- export: rendering round-trips ---------------------------------------------------

def test_export_roundtrips_constants(tmp_path: Path) -> None:
    model = small_model(dims=6, threshold=3.0)
    written = export_artifact(saved(tmp_path, model), out_dir=tmp_path / "eng",
                              plumbing=True)
    params = written[0].read_text(encoding="ascii")
    assert has_define(params, "NV_MODEL_DIMS", "6U")
    assert has_define(params, "NV_MODEL_HAS_THRESHOLD", "1")
    assert "PLUMBING EXPORT" in params

    mean_lits = FLOAT_TOKEN.findall(block(params, "nv_model_mean"))
    got_mean = np.array([float(t[:-1]) for t in mean_lits], dtype=np.float32)
    assert np.array_equal(got_mean, model.mean.astype(np.float32))

    prec_lits = FLOAT_TOKEN.findall(block(params, "nv_model_precision"))
    got_prec = np.array([float(t[:-1]) for t in prec_lits], dtype=np.float32)
    assert np.array_equal(got_prec.reshape(6, 6), model.precision.astype(np.float32))

    thr_sq = re.search(r"NV_MODEL_THRESHOLD_SQ\s+(\S+)f", params)
    assert thr_sq and np.float32(float(thr_sq.group(1))) == np.float32(9.0)


def test_export_emits_bracketing_verdicts(tmp_path: Path) -> None:
    model = small_model(dims=6, threshold=3.0)
    written = export_artifact(saved(tmp_path, model), out_dir=tmp_path / "eng",
                              plumbing=True)
    testvec = written[1].read_text(encoding="ascii")
    assert has_define(testvec, "NV_MODEL_TESTVEC_COUNT", f"{len(THRESHOLD_FACTORS)}U")
    assert has_define(testvec, "NV_MODEL_TESTVEC_HAS_VERDICTS", "1")
    verdicts = [int(t) for t in re.findall(
        r"\d", block(testvec, "nv_model_testvec_verdict"))]
    # Factors 0.0..0.99 are benign, 1.01..10.0 anomalous -- the bracket is the point.
    expected = [int(f > 1.0) for f in THRESHOLD_FACTORS]
    assert verdicts == expected
    # The float64 references round-trip exactly (%.17g).
    ref_lits = DOUBLE_TOKEN.findall(block(testvec, "nv_model_testvec_d2_ref"))
    mean32 = model.mean.astype(np.float32)
    prec32 = model.precision.astype(np.float32)
    _, refs = make_test_vectors(mean32, prec32, model.threshold)
    assert np.array_equal(np.array([float(t) for t in ref_lits]), refs)


def test_export_without_threshold_has_no_verdicts(tmp_path: Path) -> None:
    model = small_model(dims=6, threshold=None)
    written = export_artifact(saved(tmp_path, model), out_dir=tmp_path / "eng",
                              plumbing=True)
    params = written[0].read_text(encoding="ascii")
    testvec = written[1].read_text(encoding="ascii")
    assert has_define(params, "NV_MODEL_HAS_THRESHOLD", "0")
    assert has_define(testvec, "NV_MODEL_TESTVEC_HAS_VERDICTS", "0")
    assert "nv_model_testvec_verdict" not in testvec


# ---- export: the gates ---------------------------------------------------------------

def test_export_refuses_missing_threshold(tmp_path: Path) -> None:
    npz = saved(tmp_path, small_model(dims=N_DIMS, threshold=None))
    with pytest.raises(ValueError, match="no threshold"):
        export_artifact(npz, out_dir=tmp_path / "eng")


def test_export_refuses_no_holdout_fit(tmp_path: Path) -> None:
    npz = saved(tmp_path, small_model(dims=N_DIMS, holdout=None))
    with pytest.raises(ValueError, match="no-holdout"):
        export_artifact(npz, out_dir=tmp_path / "eng")


def test_export_refuses_wrong_dims(tmp_path: Path) -> None:
    npz = saved(tmp_path, small_model(dims=6))
    with pytest.raises(ValueError, match=str(N_DIMS)):
        export_artifact(npz, out_dir=tmp_path / "eng")


def test_export_refuses_malformed_artifacts(tmp_path: Path) -> None:
    base = small_model(dims=6)
    asym = base.precision.copy()
    asym[0, 1] += 1.0
    broken = MahalanobisModel(mean=base.mean, precision=asym,
                              threshold=base.threshold, meta=base.meta)
    with pytest.raises(ValueError, match="symmetric"):
        export_artifact(saved(tmp_path / "a", broken), out_dir=tmp_path / "eng",
                        plumbing=True)
    bad_mean = base.mean.copy()
    bad_mean[0] = np.nan
    broken = MahalanobisModel(mean=bad_mean, precision=base.precision,
                              threshold=base.threshold, meta=base.meta)
    with pytest.raises(ValueError, match="non-finite"):
        export_artifact(saved(tmp_path / "b", broken), out_dir=tmp_path / "eng",
                        plumbing=True)


# ---- end-to-end: compile + run the parity binary (needs a host gcc) ------------------

GCC = shutil.which("gcc")


@pytest.mark.skipif(GCC is None, reason="no host gcc -- installs enable this test")
def test_parity_binary_passes_on_fresh_export(tmp_path: Path) -> None:
    model = small_model(dims=6, threshold=3.0)
    export_artifact(saved(tmp_path, model), out_dir=tmp_path, plumbing=True)
    for src in ("mahal_score.h", "mahal_score.c", "parity_main.c"):
        shutil.copy(DEFAULT_OUT_DIR / src, tmp_path / src)
    exe = tmp_path / ("nv_parity.exe" if sys.platform == "win32" else "nv_parity")
    build = subprocess.run(
        [GCC, "-O2", "-o", str(exe), "parity_main.c", "mahal_score.c"],
        cwd=tmp_path, capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "PARITY PASS" in run.stdout
