"""
- This is the MARS CNN v2 architecture. Two inputs w/ One output:

    1. The (244, 5) record grid (grid.py) run through the MARS conv stack.
       This sees the shape of the record stream itself
    
    2. The 30 structural features (features.py), z-scored and passed through a
       small dense branch. 
       This hands the network the header/journal/entropy
       facts the raw record grid cannot see

- The two branches get concatenated and a dense head makes the call. 
- Three variants exist: 
    1. grid_only (no structural branch) 
    2. grid_struct (the main model)
    3. grid_struct_compact (smaller dense head).

- aux_mu / aux_var are the mean and variance of the structural features over the
- BENIGN training rows. 
They bake the z-scoring into the model itself so
whoever loads the model never has to carry normalization stats around.

- Training notes that shaped this model: 
    benign faults (torn tails, fresh page opens, mid-open resets) train as BENIGN so real field states 
    don't false-positive, and spec-plausible mimics stay out of the anomalous label 
    because their bytes are states the device can legitimately show, and training them as
    anomalous teaches false positives on clean logs.
"""
from __future__ import annotations

from .features import N_STRUCT


def build_mars_v2(aux_mu, aux_var, arch: str = "grid_struct"):
    """arch: grid_only | grid_struct | grid_struct_compact."""
    # keras is imported here, not at module level, so the rest of the package
    # (features, splits, generators) works without TensorFlow installed
    import keras
    from keras import layers

    grid_input = layers.Input(shape=(244, 5, 1))
    grid_branch = layers.Conv2D(32, 3, padding="same", activation="relu")(grid_input)
    grid_branch = layers.MaxPooling2D((2, 1), padding="same")(grid_branch)
    grid_branch = layers.Conv2D(64, 3, padding="same", activation="relu")(grid_branch)
    grid_branch = layers.MaxPooling2D((2, 1), padding="same")(grid_branch)
    grid_branch = layers.Conv2D(128, 3, padding="same", activation="relu")(grid_branch)
    grid_branch = layers.MaxPooling2D((2, 1), padding="same")(grid_branch)
    grid_branch = layers.Dropout(0.3)(grid_branch)
    grid_branch = layers.Flatten()(grid_branch)

    inputs = [grid_input]
    combined = grid_branch
    if arch != "grid_only":
        structural_input = layers.Input(shape=(N_STRUCT,))
        structural_branch = layers.Normalization(
            mean=aux_mu, variance=aux_var)(structural_input)
        structural_branch = layers.Dense(32, activation="relu")(structural_branch)
        combined = layers.Concatenate()([grid_branch, structural_branch])
        inputs.append(structural_input)

    dense1_units, dense2_units, dropout_rate = (
        (128, 256, 0.4) if arch == "grid_struct_compact" else (256, 512, 0.3))
    head = layers.Dense(dense1_units, activation="relu")(combined)
    head = layers.Dropout(dropout_rate)(head)
    head = layers.Dense(dense2_units, activation="relu")(head)
    head = layers.Dropout(dropout_rate)(head)
    output = layers.Dense(2, activation="sigmoid")(head)
    return keras.Model(inputs, output)
