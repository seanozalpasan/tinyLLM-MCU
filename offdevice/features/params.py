"""
Frozen feature-extraction parameters -- the SINGLE SOURCE OF TRUTH.

Every number that affects the feature output lives here. The Week 6 on-chip
CMSIS-DSP implementation MUST mirror these exactly, or the model will train on
one feature distribution and see a different one at inference time (garbage
verdicts). See CLAUDE.md "Clean audio contract".

DO NOT change a value here without (a) retraining the model and (b) re-freezing
the golden vector via offdevice/tests/make_golden.py. See offdevice/DATASET.md.
"""

import numpy as np

# --- Sample rate -------------------------------------------------------------
# The original MARS pipeline effectively computed features at 22050 Hz (the
# librosa.load default, AFTER its 48 kHz-write / 22050-reload resample). We
# compute DIRECTLY at 22050 -- no WAV, no 48 kHz write, no resample -- which
# preserves the frequency semantics (mel bins, fmax) while dropping only the
# resampling artifact. Nyquist = SR/2 = 11025 Hz, so FMAX=8000 is valid.
# Contract decision; advisor signed off 2026-06-16.
SR = 22050

# --- STFT framing (librosa defaults, set EXPLICITLY so they are frozen) ------
N_FFT = 2048
HOP_LENGTH = 512

# --- Per-feature parameters --------------------------------------------------
N_MFCC = 40          # MFCC coefficients kept
N_MELS = 40          # mel bands for the standalone melspectrogram FEATURE
FMAX = 8000          # upper mel frequency (Hz); valid since < Nyquist (11025)
N_CHROMA = 40        # chroma bins

# NOTE: librosa.feature.mfcc computes its OWN internal melspectrogram to run the
# DCT on. That internal mel filterbank uses librosa's DEFAULT n_mels=128 -- NOT
# N_MELS above. We leave it at the default to match the original preprocessing.
# N_MELS=40 applies only to the standalone melspectrogram feature. Week 6 must
# replicate BOTH: 128 internal mels for MFCC, 40 mels for the mel feature.
MFCC_INTERNAL_N_MELS = 128

# chroma_stft's `tuning` defaults to None, which makes librosa ESTIMATE a tuning
# offset from the signal on every call (librosa.estimate_tuning -> pitch track).
# That is a hidden, INPUT-DEPENDENT parameter: it would vary per dump (not
# frozen), it is meaningless for byte-derived pseudo-signals, and it would force
# Week 6 to replicate pitch-tracking on-chip. We PIN it to 0.0 -> deterministic,
# contract-stable, trivial to mirror on-chip. For the synthetic fixture
# estimate_tuning already fell back to 0.0 (the "empty frequency set" warning),
# so pinning is a no-op for the golden vector.
TUNING = 0.0

# --- Output layout -----------------------------------------------------------
# Each feature is time-averaged to a 40-vector, then stacked COLUMN-WISE into a
# 40x3 matrix in this FROZEN order. Column j corresponds to FEATURE_ORDER[j].
#
# NOTE: this is a CLEAN column-stack, deliberately replacing the original's
# np.reshape(np.vstack(...), (40, 5)), which was a row-major reflow that
# scrambled the bin/feature correspondence. Week 6 mirrors THIS clean layout,
# not the original reshape.
FEATURE_ORDER = ("mfcc", "mel", "chroma_stft")
N_BINS = 40
N_FEATURES = 3
FEATURE_SHAPE = (N_BINS, N_FEATURES)   # (40, 3)

# --- Byte -> signal contract -------------------------------------------------
# Raw dump bytes are interpreted as UNSIGNED 0..255 (uint8), widened to int16
# with NO sign-extension and NO centering (reproducing the original's
# np.array(bytearray(...), dtype=np.int16)), then scaled by /32768.0 into
# float32. Resulting range is therefore [0, 255/32768] ~= [0, 0.00778]: a small,
# all-positive, DC-heavy signal. This is intentional and MUST match on-chip.
BYTE_DTYPE = np.uint8
WIDEN_DTYPE = np.int16
SCALE = np.float32(32768.0)
SIGNAL_DTYPE = np.float32

# --- Time averaging ----------------------------------------------------------
# Each feature matrix is (n_bins, n_frames); we average over the TIME axis.
# np.mean(feature, axis=1) == np.mean(feature.T, axis=0) -> (n_bins,) vector.
TIME_AXIS = 1
