"""
These are Record-aware features for the 4 KB NV region in order to detect subtle 
tampering that the spectral approach misses.

MFCC/mel/chroma measure byte texture, and a tampered record has normal texture:
a timestamp going backwards, a humidity of 300%, a broken temp/press
correlation all look like perfectly ordinary bytes. 

These features instead measure what the spec says must be true:
    nonmonotonic_ts     ->  timestamps must be non-decreasing within a boot
    out_of_range_value  ->  each channel must be within a declared legal range (spec.CHANNELS)
    stride_break        ->  records occupy fixed 16 B slots; nothing written past
                            the first blank (parse.tail_clean)
    journal_tamper      ->  reserved0 == 0 with non-decreasing op_count along the chain
    blob / foreign      ->  unparseable pages, dirty tails, high-entropy fill

    
A violation is a fact, not a texture. Emitted as a flat float vector for the
same downstream (z-score -> model) the spectral lane uses.

Uses no FFT, no mel, no MFCC, no chroma, no spectral features at all, Making very portable for MCU.
I will say this this needs to be reconfigured to have the CNN to catch tampering it doesnt know about as well.
I don't know if this is the right way to do it, but I think it is a good start in order to have the model get the 
general idea of what tampering looks like.
"""

from __future__ import annotations

import math

import numpy as np

from collections import Counter
from offdevice.nv import spec
from offdevice.nv.parse import parse_region, records_chronological, slice_nv

FEATURE_NAMES = (
    "n_records",
    "ts_n_nonmono", "ts_min_delta", "ts_mean_delta", "ts_std_delta", "ts_max_delta",
    "temp_n_oob", "temp_max_excursion", "temp_mean", "temp_std",
    "hum_n_oob", "hum_max_excursion", "hum_mean", "hum_std",
    "press_n_oob", "press_max_excursion", "press_mean", "press_std",
    "corr_temp_hum", "corr_temp_press", "corr_hum_press",
    "jrnl_n_entries", "jrnl_reserved_bad", "jrnl_op_nonmono", "jrnl_tail_dirty",
    "pg_n_valid_headers", "pg_tail_dirty", "pg_pad_dirty", "entropy", "frac_erased",
)

N_STRUCT = len(FEATURE_NAMES)   # 30


def _safe_corr(a, b):
    """Pearson r; 0.0 when either channel is constant (undefined, not anomalous)."""
    if len(a) < 3 or np.std(a) < 1e-9 or np.std(b) < 1e-9:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _entropy(data: bytes) -> float:
    """Shannon entropy (bits/byte). Erased flash ~0; encrypted/compressed ~8."""
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def nv_struct_features(nv: bytes) -> np.ndarray:
    """4 KB NV region -> the FEATURE_NAMES vector, float32."""
    if len(nv) == spec.DUMP_OFFSET + spec.REGION_SIZE:
        nv = slice_nv(nv)
    view = parse_region(nv)
    recs = records_chronological(view)

    f: list[float] = [float(len(recs))]

    # --- timestamps: monotonic within a boot, roughly fixed cadence ---
    ts = np.array([r["ts"] for r in recs], dtype=np.float64)
    if len(ts) >= 2:
        d = np.diff(ts)
        f += [float((d <= 0).sum()), float(d.min()), float(d.mean()),
              float(d.std()), float(d.max())]
    else:
        f += [0.0, 0.0, 0.0, 0.0, 0.0]

    # --- channels: legal range is declared in the spec, so violations are facts ---
    chans = {}
    for ch in spec.CHANNELS:
        v = np.array([r[ch.name] for r in recs], dtype=np.float64)
        chans[ch.name] = v
        if len(v):
            below = np.maximum(ch.lo - v, 0).max()
            above = np.maximum(v - ch.hi, 0).max()
            n_oob = int(((v < ch.lo) | (v > ch.hi)).sum())
            f += [float(n_oob), float(max(below, above)), float(v.mean()), float(v.std())]
        else:
            f += [0.0, 0.0, 0.0, 0.0]

    # --- inter-channel physics: temp/hum/press co-vary in a real environment ---
    f += [_safe_corr(chans["temp"], chans["hum"]),
          _safe_corr(chans["temp"], chans["press"]),
          _safe_corr(chans["hum"], chans["press"])]

    # --- settings journal: reserved0 must be 0, op_count non-decreasing ---
    n_entries = reserved_bad = op_nonmono = 0
    tail_dirty = 0
    for page in view.pages:
        tail_dirty += 0 if page.journal_tail_clean else 1
        chain = [s for s in page.journal if s is not None]
        n_entries += len(chain)
        reserved_bad += sum(1 for s in chain if s.get("reserved0", 0) != 0)
        ops = [s["op_count"] for s in chain]
        op_nonmono += sum(1 for a, b in zip(ops, ops[1:]) if b < a)
    f += [float(n_entries), float(reserved_bad), float(op_nonmono), float(tail_dirty)]

    # --- page structure + raw fill ---
    n_valid = sum(1 for p in view.pages if p.header is not None)
    pg_tail_dirty = sum(1 for p in view.pages if not p.tail_clean)
    pg_pad_dirty = sum(1 for p in view.pages if not p.pad_clean)
    f += [float(n_valid), float(pg_tail_dirty), float(pg_pad_dirty)]

    f += [_entropy(nv), float(nv.count(spec.ERASED_BYTE)) / len(nv)]

    return np.array(f, dtype=np.float32)
