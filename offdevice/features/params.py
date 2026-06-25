"""
Frozen feature-extraction parameters — the single source of truth.

Every value here affects the feature output. The on-chip CMSIS-DSP port must
mirror them exactly, or the model trains on one distribution and infers on
another. Don't change a value without retraining and re-freezing the golden
vector (offdevice/tests/make_golden.py).
"""

import numpy as np

# Compute features directly at 22050 Hz — no WAV write, no 48 kHz, no resample.
# Nyquist = SR/2 = 11025 Hz, so FMAX=8000 is valid.
SR = 22050

# STFT framing — librosa defaults, pinned explicitly so they stay frozen.
N_FFT = 2048
HOP_LENGTH = 512

# Per-feature sizes (each → a 40-bin vector).
N_MFCC = 40
N_MELS = 40          # mel bands for the standalone mel feature
FMAX = 8000          # upper mel frequency (Hz); < Nyquist
N_CHROMA = 40

# GOTCHA: librosa.feature.mfcc builds its OWN internal mel filterbank for the DCT
# at librosa's default n_mels=128 — NOT N_MELS above. The on-chip port must
# replicate BOTH: 128 internal mels for MFCC, 40 for the standalone mel feature.
MFCC_INTERNAL_N_MELS = 128

# Pin chroma tuning. The default (None) makes librosa estimate a per-signal pitch
# offset every call — nondeterministic, meaningless for byte-derived signals, and
# painful to mirror on-chip. 0.0 is deterministic and a no-op on our inputs.
TUNING = 0.0

# Output: each feature time-averaged to a 40-vector, then column-stacked in this
# frozen order → (40, 3). Clean column-stack; do NOT reproduce MARS's original
# np.reshape(np.vstack(...)) row-major reflow, which scrambled the bin/feature
# correspondence. The on-chip port mirrors THIS layout.
FEATURE_ORDER = ("mfcc", "mel", "chroma_stft")
N_BINS = 40
N_FEATURES = 3
FEATURE_SHAPE = (N_BINS, N_FEATURES)   # (40, 3)

# Byte → signal contract: each dump byte as UNSIGNED 0..255, widened to int16 (no
# sign-extend, no centering), then /32768 → float32. Range [0, 255/32768] ≈
# [0, 0.00778]: small, all-positive, DC-heavy. Must match on-chip.
BYTE_DTYPE = np.uint8
WIDEN_DTYPE = np.int16
SCALE = np.float32(32768.0)
SIGNAL_DTYPE = np.float32

# Feature matrices are (n_bins, n_frames); average over the time axis.
TIME_AXIS = 1
