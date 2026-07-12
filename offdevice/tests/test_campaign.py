"""
Tests for offdevice/data/campaign.py -- the pure scheduling math only.

The orchestrator's I/O (board resets, serial capture) reuses collect.py, which
the campaign cannot exercise without hardware; what CAN be pinned is the ring
arithmetic that times the cycle-extreme chains, the ring-state reader on the
golden fixture, and the schedule constants' internal consistency (each pin
encodes a claim the runbook makes to Sean).
"""

from __future__ import annotations

from offdevice.data.campaign import (
    CHAIN_INTERVAL_S,
    CHAIN_TARGET_USED,
    FILL_COUNT,
    FILL_INTERVAL_S,
    PAGE_CYCLE_S,
    PAGE_RECORDS,
    PERIOD_S,
    TOPUP_DELAYS_S,
    ring_state,
    seconds_until_band,
)
from offdevice.nv import spec
from offdevice.nv.parse import parse_region
from offdevice.tests import fixtures


# ---- band-wait arithmetic ------------------------------------------------------


def test_band_wait_zero_when_already_at_target():
    assert seconds_until_band(CHAIN_TARGET_USED, 0.0) == 0.0


def test_band_wait_counts_up_to_target():
    # used=40 -> 78 records to go -> 78 * 15 s.
    assert seconds_until_band(40, 0.0) == (CHAIN_TARGET_USED - 40) * PERIOD_S


def test_band_wait_wraps_past_rotation():
    # used=120 is past the target: wait rolls through the next rotation.
    assert seconds_until_band(120, 0.0) == (CHAIN_TARGET_USED - 120 + PAGE_RECORDS) * PERIOD_S


def test_band_wait_subtracts_elapsed_and_rolls_to_next_cycle():
    base = (CHAIN_TARGET_USED - 40) * PERIOD_S
    # Parsed 20 minutes ago, past this cycle's band: wait lands in the next cycle.
    assert seconds_until_band(40, base + 30.0) == PAGE_CYCLE_S - 30.0
    # Elapsed exactly equal to the base wait: start now.
    assert seconds_until_band(40, base) == 0.0


def test_band_wait_never_negative_for_huge_elapsed():
    assert seconds_until_band(50, 10 * PAGE_CYCLE_S + 17.0) >= 0.0


# ---- ring-state reader on the golden fixture --------------------------------------


def test_ring_state_reads_the_fixture():
    rv = parse_region(fixtures.synthetic_nv_region())
    used, total, post_wrap = ring_state(rv)
    assert used == fixtures.PAGE1_RECORDS
    assert total == spec.RECORDS_PER_PAGE + fixtures.PAGE1_RECORDS
    assert post_wrap is True   # the fixture's other page is full and valid


def test_ring_state_on_blank_region():
    blank = b"\xff" * spec.REGION_SIZE
    assert ring_state(parse_region(blank)) == (0, 0, False)


# ---- schedule-constant claims (each one is a runbook promise) ----------------------


def test_fill_walk_spans_one_full_ring_turnover():
    # The last fill capture must land at or past the wrap, or the walk never
    # produces a just-wrapped sample.
    walk_span_s = FILL_INTERVAL_S * (FILL_COUNT - 1)
    assert walk_span_s >= spec.RECORDS_TOTAL * PERIOD_S


def test_topup_captures_stay_near_empty():
    # Delays are sequential from the fresh erase; even the second capture must
    # sit at or under the 61-record near-empty line.
    assert sum(TOPUP_DELAYS_S) / PERIOD_S <= 61


def test_chain_interval_drift_cannot_skip_the_band():
    # Each chain capture drifts forward by the interval's excess over one page
    # cycle; a drift of six or more records could hop the 238..244 band whole.
    drift_records = (CHAIN_INTERVAL_S - PAGE_CYCLE_S) / PERIOD_S
    assert 0 < drift_records < 6
