# Ported from offdevice/exam/gen_faults.py (branch CNN, 2026-07-20) so mars_v2
# carries no exam code dependency; the exam's battery CLI stays where it is.
"""Benign operational faults: field states the DEVICE ITSELF can produce.

Used as benign training examples (fill/fault hardening) -- expected verdict is
benign for every kind; flagging one is a false positive.

  torn_tail        reset inside the last record's program window: hum+press
                   read erased, ts/temp intact
  torn_before_seam same tear, but mid-page with a boot-reset landing after it
  mid_write_gap    the OLDER page's final record torn, newer page continues
  fresh_page_open  snapshot right after page-open: header + J0, zero records
  mid_open_reset   reset between header program and J0 program: header valid,
                   journal fully blank, zero records
"""
from __future__ import annotations

import random

from offdevice.nv import spec
from offdevice.nv.parse import journal_chain, parse_region

FAULT_KINDS = ("torn_tail", "torn_before_seam", "mid_write_gap",
               "fresh_page_open", "mid_open_reset")


def _tear(buf: bytearray, page_off: int, slot: int) -> None:
    off = page_off + spec.RECORDS_OFFSET + slot * spec.RECORD_SIZE
    buf[off + 8: off + 16] = bytes([spec.ERASED_BYTE]) * 8


def make_fault(nv: bytes, kind: str, rng: random.Random) -> bytes | None:
    b = bytearray(nv)
    view = parse_region(nv)
    if view.current is None:
        return None
    cur = view.current
    cur_off = cur * spec.PAGE_SIZE
    page = view.pages[cur]
    n = len(page.records)

    if kind == "torn_tail":
        if n < 3:
            return None
        _tear(b, cur_off, n - 1)

    elif kind == "torn_before_seam":
        # find a record whose successor lands at a boot seam, tear it
        for k in range(n - 1):
            if page.records[k + 1]["ts"] <= 30:
                _tear(b, cur_off, k)
                return bytes(b)
        return None

    elif kind == "mid_write_gap":
        other = 1 - cur
        old = view.pages[other]
        if old.header is None or len(old.records) != spec.RECORDS_PER_PAGE:
            return None
        _tear(b, other * spec.PAGE_SIZE, spec.RECORDS_PER_PAGE - 1)

    elif kind == "fresh_page_open":
        # header + J0 only: needs a J0-only journal (a settings entry past J0
        # would then sit outside the empty record window -- a different state)
        if len(journal_chain(page)) != 1 or view.pages[1 - cur].header is None:
            return None
        start = cur_off + spec.RECORDS_OFFSET
        end = cur_off + spec.PAGE_SIZE
        b[start:end] = bytes([spec.ERASED_BYTE]) * (end - start)

    elif kind == "mid_open_reset":
        if view.pages[1 - cur].header is None:
            return None
        start = cur_off + spec.JOURNAL_OFFSET
        end = cur_off + spec.PAGE_SIZE
        b[start:end] = bytes([spec.ERASED_BYTE]) * (end - start)

    else:
        raise ValueError(f"unknown fault kind {kind!r}")
    return bytes(b)
