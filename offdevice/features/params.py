"""
Frozen feature-extraction parameters — the single source of truth.

Re-tuned for the 4 KB NV window (the mutable flash region the one-class model
monitors); the old 256 KB whole-image parameters are retired. Every value here
affects the feature output. The on-chip CMSIS-DSP port must mirror them exactly,
or the model trains on one distribution and infers on another. Don't change a
value without re-fitting and re-freezing the golden vector
(offdevice/tests/make_golden.py).
"""

import numpy as np

# The pipeline is defined for exactly the 4 KB NV region (== nv.spec.REGION_SIZE;
# test_features ties them). Feeding anything else — e.g. a whole 256 KB capture —
# is a caller bug, not a bigger window.
WINDOW_BYTES = 4096

# Compute features directly at 22050 Hz — no WAV write, no 48 kHz, no resample.
# The rate is a fiction for byte-derived signals; it only scales the mel/chroma
# frequency mappings, and is kept from the original contract. Nyquist = SR/2 =
# 11025 Hz, so FMAX=8000 is valid.
SR = 22050

# STFT framing, sized for a 4096-sample window: 1 + 4096//128 = 33 frames
# (librosa center=True). GOTCHA: n_fft=256 looks tempting but its ~86 Hz bin
# width is WIDER than the lowest 40-band mel filters (~74 Hz at SR=22050),
# leaving empty filters => constant feature rows => zero-variance dimensions
# that break the Mahalanobis covariance. 512 (~43 Hz bins) is the minimum
# healthy size.
N_FFT = 512
HOP_LENGTH = 128

# Per-feature sizes (each → a 40-bin vector).
N_MFCC = 40
N_MELS = 40          # mel bands for the standalone mel feature
FMAX = 8000          # upper mel frequency (Hz); < Nyquist
N_CHROMA = 40

# MFCC's internal mel filterbank, passed to librosa explicitly. librosa's default
# (128 bands) needs n_fft ≳ 1700 to keep every filter non-empty — degenerate at
# our window. Pinning it to N_MELS/FMAX makes the internal filterbank IDENTICAL
# to the standalone mel feature's, so the on-chip port computes ONE 40-band mel
# spectrogram and derives both features from it.
MFCC_INTERNAL_N_MELS = 40
MFCC_FMAX = 8000

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
# [0, 0.00778]: small, all-positive, DC-heavy. One byte = one sample, so the 4 KB
# window is 4096 samples. Must match on-chip.
BYTE_DTYPE = np.uint8
WIDEN_DTYPE = np.int16
SCALE = np.float32(32768.0)
SIGNAL_DTYPE = np.float32

# Feature matrices are (n_bins, n_frames); average over the time axis.
TIME_AXIS = 1
