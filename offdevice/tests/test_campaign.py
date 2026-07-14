"""
Tests for offdevice/data/campaign.py -- the pure scheduling math only.

The orchestrator's I/O (board resets, serial capture) reuses collect.py, which
the campaign cannot exercise without hardware; what CAN be pinned is the ring
arithmetic that times the cycle-extreme chains, the ring-state reader on the
golden fixture, and the schedule constants' internal consistency (each pin
encodes a claim the runbook makes to Sean). The chain-walk tests simulate the
legs' LANDINGS under the drift hardware actually shows, not just the design
constants -- the old open-loop chain passed a constants-only test and then
walked straight past the band on real captures.
"""

from __future__ import annotations

from offdevice.data.campaign import (
    BAND_HI,
    BAND_LO,
    CHAIN_TARGETS_USED,
    FILL_COUNT,
    FILL_INTERVAL_S,
    MIN_CAPTURE_GAP_S,
    PAGE_CYCLE_S,
    PAGE_RECORDS,
    PERIOD_S,
    TOPUP_DELAYS_S,
    ring_state,
    seconds_until_used,
)
from offdevice.nv import spec
from offdevice.nv.parse import parse_region
from offdevice.tests import fixtures

# The just-after-rotation window: the totals right after a page rotation drops
# the ring from 244 back to 123 -- the cycle's other undersampled extreme.
ROTATION_LO = spec.RECORDS_PER_PAGE + 1
ROTATION_HI = spec.RECORDS_PER_PAGE + 8


# ---- leg-wait arithmetic -----------------------------------------------------------


def test_leg_wait_zero_when_already_at_target():
    assert seconds_until_used(116, 116, 0.0) == 0.0


def test_leg_wait_counts_up_to_target():
    assert seconds_until_used(40, 116, 0.0) == (116 - 40) * PERIOD_S


def test_leg_wait_wraps_past_rotation():
    # used=120 is past a 116 target: the wait rolls through the next rotation.
    assert seconds_until_used(120, 116, 0.0) == (116 - 120 + PAGE_RECORDS) * PERIOD_S


def test_leg_wait_subtracts_elapsed_and_rolls_to_next_cycle():
    base = (116 - 40) * PERIOD_S
    # Parsed a while ago, past this cycle's target: wait lands in the next cycle.
    assert seconds_until_used(40, 116, base + 30.0) == PAGE_CYCLE_S - 30.0
    # Elapsed exactly equal to the base wait: start now.
    assert seconds_until_used(40, 116, base) == 0.0


def test_leg_wait_never_negative_for_huge_elapsed():
    assert seconds_until_used(50, 116, 10 * PAGE_CYCLE_S + 17.0) >= 0.0


def test_leg_wait_min_wait_rolls_whole_page_cycles():
    # A target two records ahead is only seconds away; with the between-legs
    # floor the wait must roll to the SAME slot on a later cycle -- different
    # page contents under the same ring position, never a nearer shortcut.
    raw = seconds_until_used(118, 120, 8.0)
    floored = seconds_until_used(118, 120, 8.0, min_wait_s=PAGE_CYCLE_S / 2.0)
    assert floored >= PAGE_CYCLE_S / 2.0
    assert (floored - raw) % PAGE_CYCLE_S == 0.0


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


# ---- chain-walk claims (the test the open-loop chain never had) --------------------


def _landed_total(target_used: int, err: int) -> int:
    """Ring total after a leg lands err records past its target on a wrapped ring."""
    total = PAGE_RECORDS + target_used + err
    if total > spec.RECORDS_TOTAL:      # crossed the rotation: the oldest page dropped
        total -= PAGE_RECORDS
    return total


def test_chain_targets_are_band_walk_then_rotation():
    *band_targets, rot_target = CHAIN_TARGETS_USED
    assert list(band_targets) == sorted(band_targets)
    assert all(BAND_LO <= PAGE_RECORDS + t <= BAND_HI for t in band_targets)
    assert ROTATION_LO <= PAGE_RECORDS + rot_target <= ROTATION_HI


def test_chain_legs_land_wanted_states_under_real_drift():
    # Hardware lands legs PAST their targets (reboot re-phasing + the RC-derived
    # ms tick; ~+2 records measured). Every landing must stay a wanted state --
    # in-band, full ring, or just-after-rotation -- across the whole plausible
    # error range, not only at the design point.
    for err in range(4):
        landings = [_landed_total(t, err) for t in CHAIN_TARGETS_USED]
        assert all(BAND_LO <= t <= BAND_HI or ROTATION_LO <= t <= ROTATION_HI
                   for t in landings), (err, landings)


def test_chain_walks_the_band_not_just_touches_it():
    # A chain must bank at least three in-band captures across the plausible
    # landing errors -- the measured ~+2, the design point, and one record
    # SHORT (a slower board's RC tick) -- the coverage the sizing note prices
    # at a five-capture minimum per campaign.
    for err in (-1, 0, 1, 2):
        in_band = sum(BAND_LO <= _landed_total(t, err) <= BAND_HI
                      for t in CHAIN_TARGETS_USED)
        assert in_band >= 3, (err, in_band)


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


def test_min_capture_gap_spans_records():
    # Under one record period apart, two captures freeze the same ring image --
    # the byte-identical-clone class that has bitten at phase seams twice.
    assert MIN_CAPTURE_GAP_S >= 2 * PERIOD_S
