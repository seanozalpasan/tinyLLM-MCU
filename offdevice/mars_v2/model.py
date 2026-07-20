"""OG-MARS-v2: the improvement pass -- a competitive supervised detector.

  - the 122 non-holdout, non-collab clean logs,
  - FRESH synthetic anomalies from the FIXED generator (synthesize()) over
    those 122 bases only, sandbox seed 4242 (never used anywhere else),
  - sandbox benign-fault examples (torn tails etc.) from those same bases.

  It never reads the 16 real anomalies, the 31 holdout logs, the Phase-3
battery, or the benign-fault battery; those are graded exactly once by
`grade`, after `freeze`.

Levers used (documented for the report):
    1. Volume + diversity: ~1300 sandbox anomalies over the fixed generator's
        full type list (header + rollback-era types included) with wide random
        magnitudes/placements/content, stratified across fill x settings regimes
        by using every sandbox base. Spec-PLAUSIBLE mimics (ts_seam_mimic,
        journal chain_end_mimic, all rollback modes) are EXCLUDED from the
        anomalous label: their bytes are states the device can legitimately show,
        and training them as anomalous teaches false positives on benign logs.
        They stay designed misses.
    2. Richer input: two branches -- the V2 grid (nv_grid_v2: seam-aware dt,
        torn tolerance) convolved by the MARS conv stack, concatenated with the
        30-dim nv_struct feature vector (z-scored), giving the network the
        header/journal/entropy signals the raw record grid structurally cannot
        see (Part A finding 3). Reported as grid+structural, not grid-only.
    3. Architecture search (sandbox-internal): grid-only vs grid+struct vs a
        compact grid+struct variant, 2 seeds each; winner by held-out sandbox
        validation recall at a 2%-FPR-calibrated threshold.
    4. Calibrated threshold: the (1 - 0.02) 'higher' quantile of p(anomalous)
        over the STRATIFIED benign validation split (never fit on) -- fit.py's
        estimator, not the 0.5 default.
    5. Benign-fault hardening: sandbox torn-tail / page-open / mid-open-reset
        examples train as BENIGN so field states don't false-positive.

    python -m offdevice.exam.og_mars_v2 gen      # sandbox data (fast)
    python -m offdevice.exam.og_mars_v2 select   # candidate search (slow)
    python -m offdevice.exam.og_mars_v2 freeze   # pick winner, update frozen.json
    python -m offdevice.exam.og_mars_v2 grade    # ONE exam pass (v2 + baseline)
"""
from __future__ import annotations


import keras
from keras import layers
from .features import N_STRUCT

def build_mars_v2(aux_mu, aux_var, arch: str = "grid_struct"):
    """arch: grid_only | grid_struct | grid_struct_compact (exam candidates)."""


    gin = layers.Input(shape=(244, 5, 1))
    x = layers.Conv2D(32, 3, padding="same", activation="relu")(gin)
    x = layers.MaxPooling2D((2, 1), padding="same")(x)
    x = layers.Conv2D(64, 3, padding="same", activation="relu")(x)
    x = layers.MaxPooling2D((2, 1), padding="same")(x)
    x = layers.Conv2D(128, 3, padding="same", activation="relu")(x)
    x = layers.MaxPooling2D((2, 1), padding="same")(x)
    x = layers.Dropout(0.3)(x)
    x = layers.Flatten()(x)
    inputs = [gin]
    if arch != "grid_only":
        ain = layers.Input(shape=(N_STRUCT,))
        a = layers.Normalization(mean=aux_mu, variance=aux_var)(ain)
        a = layers.Dense(32, activation="relu")(a)
        x = layers.Concatenate()([x, a])
        inputs.append(ain)
    d1, d2, drop = ((128, 256, 0.4) if arch == "grid_struct_compact"
                    else (256, 512, 0.3))
    x = layers.Dense(d1, activation="relu")(x)
    x = layers.Dropout(drop)(x)
    x = layers.Dense(d2, activation="relu")(x)
    x = layers.Dropout(drop)(x)
    out = layers.Dense(2, activation="sigmoid")(x)
    return keras.Model(inputs, out)
