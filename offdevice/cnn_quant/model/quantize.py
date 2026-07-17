"""
Post-training quantization of the MARS CNN: trained .keras -> float32 + int8 .tflite.

Pipeline:
    mars_cnn_<mode>.keras
        -> TFLiteConverter (float32)                      -> baseline size
        -> TFLiteConverter (int8 + representative_dataset) -> size, dtypes, shrink

The int8 path calibrates on real training features (representative_dataset) so the
converter can pick each tensor's scale/zero-point, and forces int8 I/O for the MCU.
describe() uses the BUILTIN_REF resolver so dtypes reflect TFLM's reference kernels,
not the XNNPACK delegate.

NOTE: the model is a constant predictor -- these are ENGINEERING numbers
(size, shrink, Proof of Concept), not detection numbers.
"""

import keras
import numpy as np
import tensorflow as tf

from offdevice.cnn_quant.model.dataset import build_dataset
from offdevice.cnn_quant.model.z_normal import apply_normalizer
from offdevice.cnn_quant.features import params
from offdevice.cnn_quant.paths import CSV, BINS
from pathlib import Path

# ---- config -----------------------------------------------------------------
HERE = Path(__file__).resolve().parent
THRESHOLD = 0.9

# ---- conversion -------------------------------------------------------------
def make_representative_dataset(x_train):
    def gen():
        for sample in x_train:
            yield [np.expand_dims(sample.astype(np.float32), axis=0)]
    return gen

def convert_int8(model, x_train):                    
    c = tf.lite.TFLiteConverter.from_keras_model(model)
    c.optimizations = [tf.lite.Optimize.DEFAULT]
    c.representative_dataset = make_representative_dataset(x_train)   # <- was the global rep func
    c.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    c.inference_input_type  = tf.int8
    c.inference_output_type = tf.int8
    return c.convert()

def convert_float32(model):
    # No optimizations, no representative dataset -> unambiguously float32 baseline.
    return tf.lite.TFLiteConverter.from_keras_model(model).convert()


# ---- Write Model --------------------------------------------------------------------
def write_model(tflite_bytes, suffix, out_dir=HERE / "quantized"):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"mars_cnn_{suffix}.tflite"
    path.write_bytes(tflite_bytes)
    size = path.stat().st_size
    print(f"{path}: {size} bytes")
    return path, size


# ---- inspection -------------------------------------------------------------
def describe(path, label):
    # BUILTIN_REF forces the reference kernels TFLM implements. The default
    # silently attaches the XNNPACK delegate (shows up as a phantom DELEGATE op),
    # so Proof of Concept/bit-exactness numbers would compare against the wrong kernels.
    interp = tf.lite.Interpreter(
        model_path=str(path),
        experimental_op_resolver_type=tf.lite.experimental.OpResolverType.BUILTIN_REF,
    )
    interp.allocate_tensors()
    inp, out = interp.get_input_details()[0], interp.get_output_details()[0]
    dtypes = sorted({np.dtype(t['dtype']).name for t in interp.get_tensor_details()})

    print(f"\n{path}")
    print(f"  input dtype       : {np.dtype(inp['dtype']).name}")
    print(f"  output dtype      : {np.dtype(out['dtype']).name}")
    print(f"  all tensor dtypes : {dtypes}")

    info = {
        "model": label,
        "input dtype": np.dtype(inp['dtype']).name,
        "output dtype": np.dtype(out['dtype']).name,
        "all tensor dtypes": ", ".join(dtypes),
    }
    return interp, info

def _quantize(x, detail):      # float to int8, using the tensor's scale + zero-point
    scale, zero = detail["quantization"]
    return np.clip(np.round(x / scale + zero), -128, 127).astype(np.int8)

def _dequantize(y, detail):    # int8 to float
    scale, zero = detail["quantization"]
    return (y.astype(np.float32) - zero) * scale

def tflite_predict(interp, samples):
    """Run each sample through a tflite interpreter, returning float32 outputs.

    Transparent to precision: a float32 model takes/returns floats directly; an
    int8 model gets its input quantized and output dequantized at the boundary.
    """
    inp = interp.get_input_details()[0]
    out = interp.get_output_details()[0]
    outputs = []

    for sample in samples:
        x = np.expand_dims(sample.astype(np.float32), axis=0)   # add batch dim

        if inp["dtype"] == np.int8:
            x = _quantize(x, inp)

        interp.set_tensor(inp["index"], x)
        interp.invoke()
        y = interp.get_tensor(out["index"])[0]

        if out["dtype"] == np.int8:
            y = _dequantize(y, out)

        outputs.append(y.astype(np.float32))

    return np.array(outputs)

def mars_class(raw):
    """MARS decision rule: sigmoid > 0.9 on column 1."""
    return (raw[:, 1] > THRESHOLD).astype(int)


# ---- run --------------------------------------------------------------------
if __name__ == "__main__":
    # 1. load the trained model + data (x_train calibrates int8, x_test checks Proof of Concept)
    # mode-derived name: was hard-coded "baseline", which silently quantized the
    # wrong model after an ACTIVE_MODE switch (features would mismatch too).
    model = keras.models.load_model(
        HERE / f"mars_cnn_{params.ACTIVE_MODE.lower()}.keras", compile=False)
    x_train, _, x_test, _ = build_dataset(CSV, BINS)

    stats = np.load(HERE / "norm_stats.npz")          
    mu, sd = stats["mu"], stats["sd"]                 
    x_train = apply_normalizer(x_train, mu, sd)       
    x_test  = apply_normalizer(x_test,  mu, sd)       

    # 2. convert to both precisions, report size
    path_f32, size_f32 = write_model(convert_float32(model), "float32")
    path_i8, size_i8 = write_model(convert_int8(model, x_train), "int8")
    print(f"\nsize:  float32 {size_f32/1024:.1f} KB  ->  int8 {size_i8/1024:.1f} KB"
          f"   ({size_f32/size_i8:.2f}x smaller)")

    # 3. confirm what actually got quantized (dtypes)
    interp_f32, _ = describe(path_f32, "float32")
    interp_i8,  _ = describe(path_i8,  "int8")

    # 4. Proof of Concept: does int8 give the same answer as float32 on the same inputs?
    f32_raw = tflite_predict(interp_f32, x_test)
    i8_raw  = tflite_predict(interp_i8,  x_test)
    agree   = np.mean(mars_class(i8_raw) == mars_class(f32_raw)) * 100

    # 5. summary
    print("\n===== SUMMARY =====")
    print(f"  held-out samples         : {len(x_test)}")
    print(f"  class agreement int8/f32 : {agree:.1f}%")
    print(f"  max  |int8 - f32| output : {np.abs(i8_raw - f32_raw).max():.6f}")
    print(f"  mean |int8 - f32| output : {np.abs(i8_raw - f32_raw).mean():.6f}")
    print(f"  size  float32 / int8     : {size_f32/1024:.1f} KB / {size_i8/1024:.1f} KB")
    print(f"  size ratio float32/int8  : {size_f32/size_i8:.2f}x")
    print("\n  NOTE: constant-predictor model -- engineering numbers, not detection.")