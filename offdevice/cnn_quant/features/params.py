"""
Feature params for the MARS CNN lane

BASELINE  = original MARS: whatever preprocessing.py did, including librosa
            defaults it never overrode. 
OPTIMIZED = our variant, shaped by what the M33 can compute.

The modes disagree on most values, so those live in the branches 
Only mode-independent ones are shared.
"""

import numpy as np

ACTIVE_MODE = "BASELINE"

# MARS read the whole 256 KB NS bank as one window (EXPECTED_MEM_SIZE = 262144).
REGION_BYTES = 0x40000

if ACTIVE_MODE == "BASELINE":
    WINDOW_BYTES = REGION_BYTES
    WINDOW_STRIDE = REGION_BYTES
    FEATURE_ORDER = ("mfcc", "mel", "chroma_stft", "chroma_cqt", "chroma_cens")

    N_FFT = 2048          # librosa default
    HOP_LENGTH = 512

    # MARS's mfcc() got no n_mels, so its internal bank is librosa's 128 
    MFCC_INTERNAL_N_MELS = 128

    TUNING = None         # librosa default: estimated per signal
    MARS_WAV_RESAMPLE = True

    # MARS fed the CNN raw features so no z-score
    # Flip to True to quantize this mode: int8 on raw features destroys it 
    NORMALIZE = False

    # MARS's vstack->reshape(40,5) is a C-order reinterpretation:
    # rows 0-7 are mfcc, 8-15 mel, etc. 
    ASSEMBLY = "mars_reshape"

elif ACTIVE_MODE == "OPTIMIZED":
    WINDOW_BYTES = 49152
    WINDOW_STRIDE = 49152

    FEATURE_ORDER = ("mfcc", "mel", "chroma_stft")   # CQT bank won't port to CMSIS-DSP

    N_FFT = 512           # ~4x cheaper per frame than 2048 on the M33
    HOP_LENGTH = 160

    MFCC_INTERNAL_N_MELS = 40   # lets mfcc + mel share one bank; port computes it once
    TUNING = 0.0                # estimating it means porting librosa's piptrack

    # On, to match BASELINE's signal so mode deltas are attributable to features/
    # windows/assembly. Port cost: 22050/48000 = 147/320, rational polyphase, not
    # integer decimation. If too slow on the M33, flip it AND re-run the comparison.
    MARS_WAV_RESAMPLE = True

    ASSEMBLY = "stack"          # column j is feature j

    NORMALIZE = True            # int8 needs it: raw features -> 10/256 codes used

else:
    raise ValueError(f"unknown ACTIVE_MODE {ACTIVE_MODE!r}")


# --- SHARED ---

# Resample target in both modes: bytes are declared WAV_SR "audio" and decimated
# to SR. 
SR = 22050

N_MFCC = 40
N_MELS = 40            # the standalone mel feature, not MFCC_INTERNAL_N_MELS
N_CHROMA = 40
BINS_PER_OCTAVE = 40   # chroma_cqt/cens only

FMAX = 8000            # MEL ONLY -- MARS passed no fmax to mfcc. Must stay < SR/2.

WAV_SR = 48000         # what MARS declared its wav to be
RES_TYPE = "soxr_hq"   # librosa.load's default in 0.11.0

N_BINS = N_MFCC
N_FEATURES = len(FEATURE_ORDER)
FEATURE_SHAPE = (N_BINS, N_FEATURES)

# The reshape only means anything for 5x40 -> (40,5); with 3 features it would
# silently produce a layout nobody reasoned about.
if ASSEMBLY == "mars_reshape" and N_FEATURES != 5:
    raise ValueError(f"mars_reshape needs 5 features, FEATURE_ORDER has {N_FEATURES}")

# byte -> signal: each byte UNSIGNED 0..255 -> int16 -> /32768 -> float32.
# Range ~[0, 0.0078]. 1 byte = 1 sample until the resample breaks that.
BYTE_DTYPE = np.uint8
WIDEN_DTYPE = np.int16
SCALE = np.float32(32768.0)
SIGNAL_DTYPE = np.float32

TIME_AXIS = 1          # features are (n_bins, n_frames); MARS's mean(X.T, axis=0)
