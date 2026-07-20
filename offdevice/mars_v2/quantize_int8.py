"""
Full-integer (int8) export of the two-input mars_v2 model for the target device.

TFLite quantizes multi-input models fine -- the representative dataset just has
to yield both inputs together. The numerics risk specific to THIS model: the
structural input mixes tiny features (correlations around 1) with huge ones
(pressure means around 100,000), and int8 gives one scale per input tensor, so
the small features can get crushed to a couple of quantization steps. Whether
that actually hurts is an empirical question, so this script measures it:

  1. converts with a representative dataset drawn from the fit-split negative logs
  2. scores the negative bank + freshly synthesized positives through BOTH engines
  3. reports verdict agreement at the shipped threshold, score drift, and the
     per-input quantization scales (the smoking gun if agreement is bad)

Two modes:

  embedded norm (default)  the model's own Normalization layer z-scores inside
                           the graph, so the structural input is quantized in
                           raw feature units and the huge features force a
                           coarse scale (~400) that flattens the small ones.

  --external-norm          the Normalization layer is stripped from the graph
                           (weights untouched, nothing re-fit) and its exact
                           z-scoring is applied in float BEFORE quantization,
                           so the structural input is quantized in z-units
                           (range ~ +/-5). The stats are exported to
                           weights/mars_v2_norm.json so the device can
                           reproduce the same float pre-step. A float parity
                           gate (stripped model on standardized inputs vs
                           original model on raw inputs) must pass before any
                           quantization happens.

    python -m offdevice.mars_v2.quantize_int8 [--external-norm]
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import numpy as np

from .anomalies import synthesize as synth_positive, SYNTH_TYPES as SYNTH_CLASSES
from .features import FEATURE_NAMES, nv_struct_features
from .grid import nv_grid_v2
from .paths import META_JSON, WEIGHTS_DIR
from .splits import stratified_calib_split, trainable_files

TRAINING_SEED = 4242            # reproduces the model's own fit/val split
POSITIVE_SEED = 7100            # dev-only seed for the agreement-check positives
POSITIVES_PER_CLASS = 12
INT8_PATH = WEIGHTS_DIR / "mars_v2_int8.tflite"
EXT_INT8_PATH = WEIGHTS_DIR / "mars_v2_int8_extnorm.tflite"
NORM_JSON = WEIGHTS_DIR / "mars_v2_norm.json"
PARITY_TOL = 1e-4               # float gate: stripped model must match original


def _inputs_of(payloads: list[bytes]) -> tuple[np.ndarray, np.ndarray]:
    grids = np.stack([nv_grid_v2(payload)
                      for payload in payloads])[..., None].astype(np.float32)
    structural = np.stack([nv_struct_features(payload)
                           for payload in payloads]).astype(np.float32)
    return grids, structural


def _synthesize_positives(bases: list[Path]) -> list[bytes]:
    rng = random.Random(POSITIVE_SEED)
    out: list[bytes] = []
    for class_type in SYNTH_CLASSES:
        made, tries = 0, 0
        while made < POSITIVES_PER_CLASS and tries < POSITIVES_PER_CLASS * 6:
            tries += 1
            base = bases[rng.randrange(len(bases))]
            from offdevice.nv.parse import slice_nv
            nv_region = slice_nv(base.read_bytes())
            result = synth_positive(nv_region, {"type": class_type}, rng)
            if result is None or result[0] == nv_region:
                continue
            out.append(result[0])
            made += 1
    return out


def _find_normalization(model):
    import keras
    candidates = [layer for layer in model.layers
                  if isinstance(layer, keras.layers.Normalization)]
    if len(candidates) != 1:
        raise RuntimeError("expected exactly one Normalization layer on the "
                           f"structural path, found {len(candidates)}")
    return candidates[0]


def _standardize(structural: np.ndarray, mean: np.ndarray, variance: np.ndarray,
                 eps: float, mode: str) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        if mode == "floor_on_std":
            denom = np.maximum(np.sqrt(variance), eps)
        else:                       # add_to_variance
            denom = np.sqrt(variance + eps)
        return ((structural.astype(np.float64) - mean) / denom).astype(np.float32)


def _norm_transform(norm, sample: np.ndarray):
    """Recover the layer's exact z-scoring: mean, variance, epsilon, mode.

    Keras versions differ on how epsilon enters -- (x-m)/sqrt(v+eps) vs
    (x-m)/max(sqrt(v), eps) -- so rather than trusting documentation, the layer
    itself is evaluated on real feature rows and the candidate formulas are
    checked against it. The winner must match to float precision.
    """
    import keras
    mean = np.asarray(norm.mean).reshape(-1).astype(np.float64)
    variance = np.asarray(norm.variance).reshape(-1).astype(np.float64)
    reference = np.asarray(norm(sample)).astype(np.float64)
    backend_eps = (float(keras.config.epsilon()) if hasattr(keras, "config")
                   else float(keras.backend.epsilon()))
    best = None
    for mode in ("floor_on_std", "add_to_variance"):
        for eps in (backend_eps, 1e-3, 1e-6, 0.0):
            manual = _standardize(sample, mean, variance, eps, mode)
            err = float(np.abs(manual - reference).max())
            if best is None or err < best[0]:
                best = (err, eps, mode)
    err, eps, mode = best
    if err > PARITY_TOL:
        raise RuntimeError("could not reproduce the Normalization layer's "
                           f"output with any candidate formula (best diff {err:g})")
    return mean, variance, eps, mode


def _strip_normalization(model, norm):
    """Same architecture with the Normalization layer bypassed (the structural
    input feeds its next layer directly); every trained layer's weights copied
    over by name. Nothing is re-fit."""
    import keras

    def clone(layer):
        if layer is norm:
            return keras.layers.Identity(name=layer.name)
        return layer.__class__.from_config(layer.get_config())

    stripped = keras.models.clone_model(model, clone_function=clone)
    for layer in stripped.layers:
        if layer.weights:
            layer.set_weights(model.get_layer(layer.name).get_weights())
    return stripped


def _write_norm_json(mean: np.ndarray, variance: np.ndarray,
                     eps: float, mode: str) -> None:
    formula = ("(x - mean) / max(sqrt(variance), epsilon)"
               if mode == "floor_on_std"
               else "(x - mean) / sqrt(variance + epsilon)")
    payload = {
        "purpose": "float pre-normalization for the --external-norm int8 model: "
                   "apply this to nv_struct_features BEFORE int8 quantization",
        "formula": formula,
        "epsilon": eps,
        "n_features": len(FEATURE_NAMES),
        "feature_names": list(FEATURE_NAMES),
        "mean": [float(v) for v in mean],
        "variance": [float(v) for v in variance],
    }
    NORM_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _int8_scores(tflite_bytes: bytes, grids: np.ndarray,
                 structural: np.ndarray) -> np.ndarray:
    import tensorflow as tf
    interpreter = tf.lite.Interpreter(model_content=tflite_bytes)
    interpreter.allocate_tensors()
    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()[0]
    # map the two model inputs to our arrays by tensor shape
    by_rank = {len(detail["shape"]): detail for detail in input_details}
    grid_detail, struct_detail = by_rank[4], by_rank[2]

    scores = np.empty(len(grids), dtype=np.float64)
    for i in range(len(grids)):
        for detail, value in ((grid_detail, grids[i:i + 1]),
                              (struct_detail, structural[i:i + 1])):
            scale, zero_point = detail["quantization"]
            quantized = np.round(value / scale + zero_point)
            quantized = np.clip(quantized, -128, 127).astype(np.int8)
            interpreter.set_tensor(detail["index"], quantized)
        interpreter.invoke()
        raw = interpreter.get_tensor(output_details["index"])[0]
        scale, zero_point = output_details["quantization"]
        scores[i] = float((raw[1].astype(np.float64) - zero_point) * scale)
    return scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="int8 export + float-agreement report for mars_v2")
    parser.add_argument("--external-norm", action="store_true",
                        help="strip the in-graph Normalization layer and z-score "
                             "the structural input in float before quantization "
                             "(stats exported to weights/mars_v2_norm.json)")
    args = parser.parse_args(argv)

    import keras
    import tensorflow as tf

    meta = json.loads(META_JSON.read_text(encoding="utf-8"))
    model = keras.models.load_model(META_JSON.parent / meta["model"], compile=False)
    threshold = float(meta["threshold"])

    fit_files, _val_files = stratified_calib_split(trainable_files(), TRAINING_SEED)
    rep_grids, rep_structural = _inputs_of([path.read_bytes() for path in fit_files])
    print(f"[int8] representative dataset: {len(fit_files)} fit-split negative logs")

    if args.external_norm:
        norm = _find_normalization(model)
        mean, variance, eps, mode = _norm_transform(norm, rep_structural)
        print(f"[int8] external norm: layer '{norm.name}' does "
              f"{'(x-m)/max(sqrt(v), eps)' if mode == 'floor_on_std' else '(x-m)/sqrt(v+eps)'}"
              f" with eps={eps:g}")
        convert_model = _strip_normalization(model, norm)

        # float parity gate: the stripped model fed manually-standardized
        # structural must reproduce the original model fed raw structural
        original_out = model.predict([rep_grids, rep_structural], verbose=0)
        rep_structural = _standardize(rep_structural, mean, variance, eps, mode)
        stripped_out = convert_model.predict([rep_grids, rep_structural], verbose=0)
        parity = float(np.abs(original_out - stripped_out).max())
        print(f"[int8] float parity (stripped+external vs original): "
              f"max abs diff {parity:.3e}")
        if parity > PARITY_TOL:
            print(f"[int8] PARITY FAILED (tolerance {PARITY_TOL:g}) -- the "
                  "rebuilt model does not match the trained one; not quantizing")
            return 1

        _write_norm_json(mean, variance, eps, mode)
        print(f"[int8] wrote {NORM_JSON.name} (mean/variance/epsilon, "
              f"{len(FEATURE_NAMES)} features)")
        int8_path = EXT_INT8_PATH
    else:
        convert_model = model
        int8_path = INT8_PATH

    def representative_dataset():
        for i in range(len(rep_grids)):
            yield [rep_grids[i:i + 1], rep_structural[i:i + 1]]

    converter = tf.lite.TFLiteConverter.from_keras_model(convert_model)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    converter.representative_dataset = representative_dataset
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    converter.inference_input_type = tf.int8
    converter.inference_output_type = tf.int8
    tflite_bytes = converter.convert()
    int8_path.write_bytes(tflite_bytes)
    print(f"[int8] wrote {int8_path.name}: {len(tflite_bytes):,} B")

    # show the per-input scales -- the resolution each input actually gets
    interpreter = tf.lite.Interpreter(model_content=tflite_bytes)
    interpreter.allocate_tensors()
    for detail in interpreter.get_input_details():
        scale, zero_point = detail["quantization"]
        label = "grid" if len(detail["shape"]) == 4 else "structural"
        print(f"[int8]   {label} input shape={list(detail['shape'])}  "
              f"scale={scale:.6g}  zero_point={zero_point}")

    # agreement: negative bank + fresh synthetic positives through both engines.
    # the float reference is always the ORIGINAL model on raw inputs.
    negative_files = trainable_files()
    negative_payloads = [path.read_bytes() for path in negative_files]
    positive_payloads = _synthesize_positives(negative_files)
    print(f"[int8] agreement set: {len(negative_payloads)} negative + "
          f"{len(positive_payloads)} synthesized positives (seed {POSITIVE_SEED})")

    all_grids, all_structural = _inputs_of(negative_payloads + positive_payloads)
    keras_scores = model.predict([all_grids, all_structural], verbose=0)[:, 1]
    if args.external_norm:
        all_structural = _standardize(all_structural, mean, variance, eps, mode)
    int8_scores = _int8_scores(tflite_bytes, all_grids, all_structural)

    keras_verdicts = keras_scores > threshold
    int8_verdicts = int8_scores > threshold
    agree = int((keras_verdicts == int8_verdicts).sum())
    total = len(keras_verdicts)
    n_negative = len(negative_payloads)
    print(f"\n[int8] verdict agreement: {agree}/{total} "
          f"({100 * agree / total:.2f}%) at threshold {threshold:.4f}")
    print(f"[int8]   negative side: keras flags {int(keras_verdicts[:n_negative].sum())}, "
          f"int8 flags {int(int8_verdicts[:n_negative].sum())}")
    print(f"[int8]   positive side: keras {int(keras_verdicts[n_negative:].sum())}"
          f"/{total - n_negative}, int8 {int(int8_verdicts[n_negative:].sum())}"
          f"/{total - n_negative}")
    drift = np.abs(keras_scores - int8_scores)
    print(f"[int8] score drift: mean={drift.mean():.4f}  max={drift.max():.4f}")
    disagreements = np.flatnonzero(keras_verdicts != int8_verdicts)
    for index in disagreements[:10]:
        kind = "negative" if index < n_negative else "positive"
        print(f"[int8]   disagreement ({kind}): keras={keras_scores[index]:.4f} "
              f"int8={int8_scores[index]:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
