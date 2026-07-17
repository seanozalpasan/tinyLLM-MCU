"""
Build the CNN training set from a metadata CSV + a folder of .bin dumps.

metadata CSV columns: File_Name, Class_ID, Type   (Type in {train, test})
Each dump -> extract_features -> (n_windows, 40, N); every window inherits the
dump's label. BASELINE = 1 window/dump, OPTIMIZED = several.
"""

import numpy as np
import pandas as pd
import keras
from pathlib import Path

from offdevice.cnn_quant.features.extract import extract_features


def build_dataset(csv_path, bins_dir):
    meta = pd.read_csv(csv_path)
    bins_dir = Path(bins_dir)

    x_train, y_train, x_test, y_test = [], [], [], []

    for _, row in meta.iterrows():
        windows = extract_features(bins_dir / row["File_Name"])   # (n, 40, N)
        windows = windows[..., np.newaxis]                        # (n, 40, N, 1)
        label = int(row["Class_ID"])

        for w in windows:                                         
            if row["Type"] == "train":
                x_train.append(w); y_train.append(label)
            else:
                x_test.append(w);  y_test.append(label)

    x_train = np.array(x_train, dtype=np.float32)
    x_test  = np.array(x_test,  dtype=np.float32)
    y_train = keras.utils.to_categorical(y_train, num_classes=2)
    y_test  = keras.utils.to_categorical(y_test,  num_classes=2)
    return x_train, y_train, x_test, y_test


if __name__ == "__main__":
    import sys
    xtr, ytr, xte, yte = build_dataset(sys.argv[1], sys.argv[2])
    print(f"x_train {xtr.shape}  y_train {ytr.shape}")
    print(f"x_test  {xte.shape}  y_test  {yte.shape}")