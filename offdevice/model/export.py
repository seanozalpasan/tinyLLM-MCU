"""
Export a fitted Mahalanobis artifact to C -- the bridge between the off-device fit
and the on-chip scorer, plus the test vectors that prove both compute the same
distance (the model-parity gate: host reference == on-chip score).

Consumes the .npz written by offdevice.model.fit and renders two GENERATED headers
into engine/ (the hand-written scorer's home; the same files move into the firmware
at the port):

    nv_model_params.h   -- mean, precision, threshold^2 as float32 constants
    nv_model_testvec.h  -- vectors with known reference distances + verdicts

The shipped model IS the float32 rounding of the fit: every constant is rounded
once, here, and the test-vector reference distances are recomputed in float64 FROM
the rounded values -- so a parity failure can only mean the scoring arithmetic
differs, never the storage rounding. score_d2_f32 mirrors engine/mahal_score.c's
loop order op-for-op in float32; the export prints its measured gap against the
float64 reference as the evidence behind the baked-in tolerance.

    python -m offdevice.model.export offdevice\\model\\artifacts\\mahalanobis.npz
"""

from __future__ import annotations

import argparse
import hashlib
import math
from pathlib import Path

import numpy as np
import numpy.typing as npt

from offdevice.model.dataset import N_DIMS
from offdevice.model.fit import MahalanobisModel, load_model

REPO_ROOT = Path(__file__).resolve().parents[2]
# engine/ is the hand-written scorer's home: generated params + parity runner are
# host-compiled there first; the port moves the same files into the firmware.
DEFAULT_OUT_DIR = REPO_ROOT / "engine"

# Test-vector targets as multiples of the threshold distance: a walk from the mean
# itself (d = 0) out to deep anomaly, with 0.99/1.01 bracketing the alarm line --
# >= 1% margin, so a last-ulp float32 wobble cannot flip an expected verdict.
THRESHOLD_FACTORS = (0.0, 0.25, 0.5, 0.9, 0.99, 1.01, 1.5, 3.0, 10.0)

# Absolute target distances for a threshold-less (plumbing) export.
PLUMBING_TARGETS = (0.0, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)

# Fixed seed: the vectors are part of the parity evidence, so re-exporting the
# same artifact must reproduce them bit-for-bit.
TESTVEC_SEED = 28278

# Provisional d^2 tolerances baked into the testvec header. The export prints the
# measured float32-mirror gap as evidence; on-chip numbers finalize them at the
# port. The hard requirement is stricter than either: verdicts must match exactly.
TESTVEC_REL_TOL = 1e-4
TESTVEC_ABS_TOL = 1e-9

_PER_LINE = 6   # float literals per generated line -- keeps the arrays diffable

VecF32 = npt.NDArray[np.float32]
MatF32 = npt.NDArray[np.float32]
VecF64 = npt.NDArray[np.float64]
MatF64 = npt.NDArray[np.float64]


# ---- the float32 host mirror ---------------------------------------------------


def score_d2_f32(mean32: VecF32, prec32: MatF32, x: VecF32) -> float:
    """Mirror of engine/mahal_score.c, op-for-op in float32 -- the host reference.

    Sequential accumulation in the exact C loop order; numpy's dot() pairwise-sums
    in a different order and would sit a few ulps off the chip. GOTCHA: a compiler
    may fuse multiply+add into one rounding (FMA) where this mirror rounds twice;
    that residual is what the test-vector tolerance absorbs.
    """
    x32 = np.asarray(x, dtype=np.float32)
    mean = np.asarray(mean32, dtype=np.float32)
    prec = np.asarray(prec32, dtype=np.float32)
    diff = x32 - mean
    d2 = np.float32(0.0)
    for i in range(diff.shape[0]):
        acc = np.float32(0.0)
        row = prec[i]
        for j in range(diff.shape[0]):
            acc += row[j] * diff[j]
        d2 += acc * diff[i]
    return float(d2)


def _d2_f64(mean64: VecF64, prec64: MatF64, x64: VecF64) -> float:
    """Float64 reference d^2 -- order-free; float64 headroom makes order moot here."""
    diff = x64 - mean64
    return float(diff @ prec64 @ diff)


# ---- test vectors ----------------------------------------------------------------


def make_test_vectors(mean32: VecF32, prec32: MatF32,
                      threshold: float | None) -> tuple[MatF32, VecF64]:
    """Deterministic parity vectors with float64-exact reference distances.

    The distance is linear along a ray from the mean -- d(mean + a*v) = a*d(mean + v)
    -- so scaling one random direction lands each target distance exactly; the
    target grid brackets the alarm line when a threshold exists (THRESHOLD_FACTORS).
    Vectors are float32-rounded FIRST and the reference recomputed from the rounded
    values, so the stored expectation is exact for the exact bytes the C side reads.
    """
    m64 = mean32.astype(np.float64)
    p64 = prec32.astype(np.float64)
    dims = m64.shape[0]
    targets = ([f * threshold for f in THRESHOLD_FACTORS] if threshold is not None
               else list(PLUMBING_TARGETS))
    rng = np.random.default_rng(TESTVEC_SEED)
    xs = np.empty((len(targets), dims), dtype=np.float32)
    refs = np.empty(len(targets), dtype=np.float64)
    for k, target in enumerate(targets):
        if target == 0.0:
            xs[k] = mean32   # diff is exactly zero on the C side too
        else:
            v = rng.standard_normal(dims)
            d0 = math.sqrt(_d2_f64(m64, p64, m64 + v))
            xs[k] = (m64 + v * (target / d0)).astype(np.float32)
        refs[k] = _d2_f64(m64, p64, xs[k].astype(np.float64))
    return xs, refs


# ---- C literal rendering ---------------------------------------------------------


def _c_float(value: float | np.floating) -> str:
    """A float32 as a C literal that parses back to the identical float32."""
    v = float(np.float32(value))
    if not math.isfinite(v):
        raise ValueError(f"non-finite value in export: {v!r}")
    text = f"{v:.9g}"   # 9 significant digits round-trip any float32
    if "." not in text and "e" not in text:
        text += ".0"    # "1" is an int literal; "1.0f" is the float
    return text + "f"


def _c_double(value: float) -> str:
    """A float64 as a C double literal that parses back identically."""
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"non-finite value in export: {v!r}")
    text = f"{v:.17g}"
    if "." not in text and "e" not in text:
        text += ".0"
    return text


def _chunk_lines(tokens: list[str], indent: str) -> list[str]:
    return [indent + ", ".join(tokens[i:i + _PER_LINE]) + ","
            for i in range(0, len(tokens), _PER_LINE)]


def _matrix_block(matrix: MatF32) -> list[str]:
    lines: list[str] = []
    for row in matrix:
        lines.append("    {")
        lines += _chunk_lines([_c_float(v) for v in row], "        ")
        lines.append("    },")
    return lines


def _fmt_meta(value: object) -> str:
    return f"{value:.4g}" if isinstance(value, float) else str(value)


# ---- header rendering ------------------------------------------------------------


def render_params_header(model: MahalanobisModel, mean32: VecF32, prec32: MatF32,
                         artifact_name: str, sha256: str, plumbing: bool) -> str:
    """The full nv_model_params.h text -- the model, as the firmware will compile it."""
    meta = model.meta
    stats = "  ".join(f"{k}={_fmt_meta(meta[k])}"
                      for k in ("n_train", "fp_target", "shrinkage")
                      if meta.get(k) is not None)
    pins = meta.get("feature_pins") or {}
    pins_line = "  ".join(f"{k}={v}" for k, v in pins.items())
    lines = [
        "/*",
        " * nv_model_params.h -- benign Mahalanobis model constants. GENERATED by",
        f" * offdevice/model/export.py from {artifact_name} -- DO NOT EDIT; re-export.",
        " *",
        f" * source artifact sha256: {sha256}",
    ]
    if stats:
        lines.append(f" * fit: {stats}")
    if pins_line:
        lines.append(f" * features: {pins_line}  nv-spec v{meta.get('nv_spec_version', '?')}")
    if meta.get("vector_order"):
        lines.append(f" * vector fill order: {meta['vector_order']}")
    if plumbing:
        lines.append(" * PLUMBING EXPORT -- NOT A REAL MODEL (export gates bypassed).")
    lines += [
        " *",
        " * The shipped model IS these float32 values -- the float64 fit rounded once,",
        " * here. Verdicts compare squared distance to NV_MODEL_THRESHOLD_SQ (no sqrt",
        " * anywhere; squaring keeps order for non-negative distances). The arrays are",
        " * static (~58 KB at 120 dims): include from ONE translation unit only",
        " * (mahal_score.c).",
        " */",
        "#ifndef NV_MODEL_PARAMS_H",
        "#define NV_MODEL_PARAMS_H",
        "",
        f"#define NV_MODEL_DIMS          {mean32.shape[0]}U",
        f"#define NV_MODEL_HAS_THRESHOLD {1 if model.threshold is not None else 0}",
    ]
    if model.threshold is not None:
        thr = _c_float(model.threshold)
        thr_sq = _c_float(model.threshold ** 2)
        lines += [
            f"#define NV_MODEL_THRESHOLD     {thr}   /* alarm line in d units (human reference) */",
            f"#define NV_MODEL_THRESHOLD_SQ  {thr_sq}   /* the on-chip compare: anomaly = d2 > this */",
        ]
    else:
        lines += [
            "#define NV_MODEL_THRESHOLD     -1.0f   /* plumbing export -- no threshold agreed */",
            "#define NV_MODEL_THRESHOLD_SQ  -1.0f",
        ]
    lines += ["", "static const float nv_model_mean[NV_MODEL_DIMS] =", "{"]
    lines += _chunk_lines([_c_float(v) for v in mean32], "    ")
    lines += ["};", "",
              "static const float nv_model_precision[NV_MODEL_DIMS][NV_MODEL_DIMS] =", "{"]
    lines += _matrix_block(prec32)
    lines += ["};", "", "#endif /* NV_MODEL_PARAMS_H */", ""]
    return "\n".join(lines)


def render_testvec_header(xs: MatF32, refs: VecF64, verdicts: list[int] | None,
                          artifact_name: str, sha256: str, plumbing: bool) -> str:
    """The full nv_model_testvec.h text -- known answers for the parity runner."""
    count, dims = xs.shape
    lines = [
        "/*",
        " * nv_model_testvec.h -- parity vectors for the exported model. GENERATED by",
        f" * offdevice/model/export.py from {artifact_name} -- DO NOT EDIT; re-export.",
        " *",
        f" * source artifact sha256: {sha256}",
    ]
    if plumbing:
        lines.append(" * PLUMBING EXPORT -- NOT A REAL MODEL (export gates bypassed).")
    lines += [
        " *",
        " * d2_ref is the float64 reference computed from the SAME float32-rounded",
        " * constants nv_model_params.h ships -- beyond-tolerance disagreement means",
        " * the scoring ARITHMETIC differs, never the storage rounding.",
    ]
    if verdicts is not None:
        lines += [
            " * Verdicts are the float32 host mirror's (score_d2_f32 in export.py);",
            " * every vector keeps >= 1% margin from the alarm line, so a last-ulp",
            " * wobble cannot flip one.",
        ]
    lines += [
        " * Include from the parity runner (parity_main.c) only.",
        " */",
        "#ifndef NV_MODEL_TESTVEC_H",
        "#define NV_MODEL_TESTVEC_H",
        "",
        f"#define NV_MODEL_TESTVEC_COUNT        {count}U",
        f"#define NV_MODEL_TESTVEC_DIMS         {dims}U   /* must equal mahal_model_dims() */",
        f"#define NV_MODEL_TESTVEC_HAS_VERDICTS {1 if verdicts is not None else 0}",
        f"#define NV_MODEL_TESTVEC_REL_TOL      {TESTVEC_REL_TOL:g}   /* provisional (see export.py) */",
        f"#define NV_MODEL_TESTVEC_ABS_TOL      {TESTVEC_ABS_TOL:g}   /* floor for the d2 == 0 vector */",
        "",
        "static const float nv_model_testvec_x[NV_MODEL_TESTVEC_COUNT][NV_MODEL_TESTVEC_DIMS] =",
        "{",
    ]
    lines += _matrix_block(xs)
    lines += ["};", "",
              "static const double nv_model_testvec_d2_ref[NV_MODEL_TESTVEC_COUNT] =", "{"]
    lines += _chunk_lines([_c_double(r) for r in refs], "    ")
    lines += ["};"]
    if verdicts is not None:
        lines += ["",
                  "/* expected mahal_is_anomaly() results (float32 host mirror) */",
                  "static const int nv_model_testvec_verdict[NV_MODEL_TESTVEC_COUNT] =",
                  "{",
                  "    " + ", ".join(str(v) for v in verdicts) + ",",
                  "};"]
    lines += ["", "#endif /* NV_MODEL_TESTVEC_H */", ""]
    return "\n".join(lines)


# ---- validation + the export itself ----------------------------------------------


def _validate(model: MahalanobisModel, plumbing: bool) -> None:
    """The export gates: a real export must come from a real, finished fit."""
    mean, precision = model.mean, model.precision
    if mean.ndim != 1 or precision.shape != (mean.shape[0], mean.shape[0]):
        raise ValueError(
            f"malformed artifact: mean {mean.shape} vs precision {precision.shape}")
    if not (np.isfinite(mean).all() and np.isfinite(precision).all()):
        raise ValueError("artifact holds non-finite values -- corrupt fit")
    if not np.allclose(precision, precision.T, rtol=1e-10, atol=0.0):
        raise ValueError("precision matrix is not symmetric -- corrupt or hand-made artifact")
    if not (np.diag(precision) > 0).all():
        raise ValueError("precision diagonal has non-positive entries -- "
                         "not a valid precision matrix")
    # `not > 0` (rather than `<= 0`) also rejects NaN. A zero threshold would
    # collapse every test-vector target to the mean and divide the verdict-margin
    # guard by zero, so this is structural, not a bypassable gate.
    if model.threshold is not None and not model.threshold > 0:
        raise ValueError(f"threshold {model.threshold!r} is not positive -- "
                         f"not a benign-distance quantile")
    if plumbing:
        return
    if mean.shape[0] != N_DIMS:
        raise ValueError(f"artifact dims={mean.shape[0]} != the feature contract's "
                         f"{N_DIMS} -- a real export must match the pipeline "
                         f"(--plumbing to bypass)")
    if model.threshold is None:
        raise ValueError("artifact has no threshold -- agree one via fit --fp-target "
                         "first (--plumbing to bypass)")
    if model.meta.get("holdout_file") is None:
        raise ValueError("artifact was fitted with --no-holdout (plumbing) -- a real "
                         "export needs a holdout-excluding fit (--plumbing to bypass)")


def export_artifact(artifact: str | Path, out_dir: str | Path = DEFAULT_OUT_DIR,
                    plumbing: bool = False) -> list[Path]:
    """Validate, round to float32, build test vectors, write both generated headers."""
    npz = Path(artifact)
    if npz.suffix != ".npz":
        npz = npz.with_suffix(".npz")
    model = load_model(npz)
    _validate(model, plumbing)

    mean32 = model.mean.astype(np.float32)
    prec32 = model.precision.astype(np.float32)
    if not (np.isfinite(mean32).all() and np.isfinite(prec32).all()):
        raise ValueError("model values overflow float32 -- the fit is unusable on-chip")

    xs, refs = make_test_vectors(mean32, prec32, model.threshold)
    mirror = np.array([score_d2_f32(mean32, prec32, x) for x in xs])
    verdicts: list[int] | None = None
    if model.threshold is not None:
        thr_sq = float(np.float32(model.threshold ** 2))
        verdicts = [int(m > thr_sq) for m in mirror]
        # The verdict expectations must sit far from the line relative to the d2
        # tolerance, or a legitimate few-ulp chip difference could flip one.
        margin = float(np.min(np.abs(mirror - thr_sq))) / thr_sq
        if margin < 10 * TESTVEC_REL_TOL:
            raise ValueError(f"a test vector sits {margin:.2g} (relative) from the "
                             f"alarm line -- too close for stable verdict "
                             f"expectations; adjust THRESHOLD_FACTORS")

    sha = hashlib.sha256(npz.read_bytes()).hexdigest()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    renders = (
        ("nv_model_params.h",
         render_params_header(model, mean32, prec32, npz.name, sha, plumbing)),
        ("nv_model_testvec.h",
         render_testvec_header(xs, refs, verdicts, npz.name, sha, plumbing)),
    )
    for name, text in renders:
        path = out / name
        with path.open("w", encoding="ascii", newline="\n") as f:
            f.write(text)
        written.append(path)

    nonzero = refs > 0
    gap = (float(np.max(np.abs(mirror[nonzero] - refs[nonzero]) / refs[nonzero]))
           if nonzero.any() else 0.0)
    thr_text = f"{model.threshold:.6g}" if model.threshold is not None else "none (plumbing)"
    print(f"[export] {npz.name}: dims={mean32.shape[0]} threshold={thr_text}")
    print(f"[export] float32 mirror vs float64 reference over {len(refs)} vectors: "
          f"max rel gap {gap:.3g} (baked tolerance {TESTVEC_REL_TOL:g})")
    if gap > TESTVEC_REL_TOL / 10:
        print("[export] WARNING: mirror gap within 10x of the tolerance -- "
              "revisit TESTVEC_REL_TOL before trusting parity verdicts")
    for path in written:
        print(f"[export] wrote {path}")
    print(f"[export] parity check, from {out}:  "
          f"gcc -O2 -o nv_parity parity_main.c mahal_score.c ; .\\nv_parity.exe")
    return written


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Export a fitted Mahalanobis artifact to C headers in engine/.")
    ap.add_argument("artifact", type=Path, help="the .npz written by offdevice.model.fit")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help=f"where the generated headers land (default {DEFAULT_OUT_DIR})")
    ap.add_argument("--plumbing", action="store_true",
                    help="permit a threshold-less / --no-holdout / off-size artifact; "
                         "the headers are stamped NOT A REAL MODEL")
    args = ap.parse_args()
    try:
        export_artifact(args.artifact, args.out_dir, plumbing=args.plumbing)
    except (ValueError, FileNotFoundError) as e:
        print(f"[export] {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
