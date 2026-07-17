"""
Shared filesystem anchors for the cnn_quant pipeline.

ONE place to repoint when the dataset moves (placeholder sample bins -> real
board captures): edit BINS, regenerate metadata.csv, and train / quantize /
svd_factor / export all follow. Three copies of these constants drifting
apart is the classic "trained on X, calibrated on Y" bug.
"""
from pathlib import Path

PKG = Path(__file__).resolve().parent

CSV = PKG / "data" / "metadata.csv"

# Placeholder bins from the original MARS sample set (262144 B each).
# Real board captures: switch to the line below, then run
#   python -m offdevice.cnn_quant.data.make_metadata   and retrain.
# BINS = Path(r"C:\MARS 2.0\ids-implementation-clean\Classification-Server-Scripts\sample_dataset\bins")
BINS = Path(r"C:\MARS 2.0\tinyLLM-MCU\dataset_captures")
