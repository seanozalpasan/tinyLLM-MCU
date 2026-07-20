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

violations becomes a fact instead of a texture. Emitted as a flat float vector for the
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


def _safe_corr(series_a, series_b):
    """Pearson r; 0.0 when either channel is constant (undefined, not anomalous)."""
    if len(series_a) < 3 or np.std(series_a) < 1e-9 or np.std(series_b) < 1e-9:
        return 0.0
    return float(np.corrcoef(series_a, series_b)[0, 1])


def _entropy(data: bytes) -> float:
    """Shannon entropy (bits/byte). Erased flash ~0; encrypted/compressed ~8."""
    if not data:
        return 0.0
    byte_counts = Counter(data)
    total = len(data)
    return -sum((count / total) * math.log2(count / total)
                for count in byte_counts.values())


def nv_struct_features(nv: bytes) -> np.ndarray:
    """4 KB NV region -> the FEATURE_NAMES vector, float32."""
    if len(nv) == spec.DUMP_OFFSET + spec.REGION_SIZE:
        nv = slice_nv(nv)
    view = parse_region(nv)
    records = records_chronological(view)

    features: list[float] = [float(len(records))]

    # --- timestamps: monotonic within a boot, roughly fixed cadence ---
    timestamps = np.array([record["ts"] for record in records], dtype=np.float64)
    if len(timestamps) >= 2:
        deltas = np.diff(timestamps)
        features += [float((deltas <= 0).sum()), float(deltas.min()),
                     float(deltas.mean()), float(deltas.std()), float(deltas.max())]
    else:
        features += [0.0, 0.0, 0.0, 0.0, 0.0]

    # --- channels: the legal range is declared in the spec, so violations are facts ---
    channel_values = {}
    for channel in spec.CHANNELS:
        values = np.array([record[channel.name] for record in records],
                          dtype=np.float64)
        channel_values[channel.name] = values
        if len(values):
            below = np.maximum(channel.lo - values, 0).max()
            above = np.maximum(values - channel.hi, 0).max()
            out_of_range_count = int(((values < channel.lo) | (values > channel.hi)).sum())
            features += [float(out_of_range_count), float(max(below, above)),
                         float(values.mean()), float(values.std())]
        else:
            features += [0.0, 0.0, 0.0, 0.0]

    # --- inter-channel physics: temp/hum/press move together in a real environment ---
    features += [_safe_corr(channel_values["temp"], channel_values["hum"]),
                 _safe_corr(channel_values["temp"], channel_values["press"]),
                 _safe_corr(channel_values["hum"], channel_values["press"])]

    # --- settings journal: reserved0 must be 0, op_count never steps backwards ---
    journal_entries = reserved_violations = op_count_backwards = 0
    journal_tails_dirty = 0
    for page in view.pages:
        journal_tails_dirty += 0 if page.journal_tail_clean else 1
        chain = [slot for slot in page.journal if slot is not None]
        journal_entries += len(chain)
        reserved_violations += sum(1 for slot in chain
                                   if slot.get("reserved0", 0) != 0)
        op_counts = [slot["op_count"] for slot in chain]
        op_count_backwards += sum(1 for earlier, later in zip(op_counts, op_counts[1:])
                                  if later < earlier)
    features += [float(journal_entries), float(reserved_violations),
                 float(op_count_backwards), float(journal_tails_dirty)]

    # --- page structure + raw fill ---
    valid_headers = sum(1 for page in view.pages if page.header is not None)
    record_tails_dirty = sum(1 for page in view.pages if not page.tail_clean)
    pads_dirty = sum(1 for page in view.pages if not page.pad_clean)
    features += [float(valid_headers), float(record_tails_dirty), float(pads_dirty)]

    features += [_entropy(nv), float(nv.count(spec.ERASED_BYTE)) / len(nv)]

    return np.array(features, dtype=np.float32)
