"""Data locations for the standalone mars_v2 package.

This is the one file that knows where anything lives on disk. While the package
sits inside the tinyLLM-MCU workspace it points at the shared data bank
(captures, holdout/quarantine lists); when the package moves to its own repo,
edit these constants (or set MARS_V2_DATA_ROOT) and nothing else changes.

Sandbox + weights are package-local on purpose: mars_v2 owns what it generates.
"""
from __future__ import annotations

import os
from pathlib import Path

PKG = Path(__file__).resolve().parent

# The shared data bank. Default: the enclosing tinyLLM-MCU workspace layout
# (PKG = <repo>/offdevice/mars_v2 -> PKG.parent = <repo>/offdevice).
_DATA_ROOT = Path(os.environ.get("MARS_V2_DATA_ROOT",
                                 PKG.parent / "data"))

CAPTURES = _DATA_ROOT / "captures"          # benign__*.bin capture bank
HOLDOUT_TXT = _DATA_ROOT / "holdout.txt"    # names graded-only, never trained on
COLLAB_TXT = _DATA_ROOT / "collab_bases.txt"
QUARANTINE_TXT = _DATA_ROOT / "quarantine.txt"

# Package-local outputs.
SANDBOX = PKG / "data" / "sandbox"          # generated training data (seeded)
SBX_MANIFEST = SANDBOX / "manifest.jsonl"
SELECTION_JSON = SANDBOX / "selection.json"
WEIGHTS_DIR = PKG / "weights"
MODEL_PATH = WEIGHTS_DIR / "mars_v2.keras"
META_JSON = WEIGHTS_DIR / "mars_v2.json"    # threshold + provenance for score.py
