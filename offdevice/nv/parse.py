"""
NV-region reader: slice the 4 KB region out of a 256 KB dump and parse it.

Byte-for-byte mirror of what the firmware's nv_logger.c writes -- both sides
obey offdevice/nv/spec.py (contract #1). Benign-strict: anything violating the
layout (foreign header, a written slot after the head) is reported, never
repaired -- in a real capture such a violation IS the class of structural
anomaly the IDS hunts.

Eyeball a capture (accepts a 256 KB dump or a bare 4 KB slice):
    python -m offdevice.nv.parse offdevice\\data\\captures\\<capture>.bin
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from offdevice.nv import spec

DUMP_SIZE = spec.DUMP_OFFSET + spec.REGION_SIZE   # the NV region ends the 256 KB image


@dataclass(frozen=True)
class PageView:
    """One parsed 2 KB page: header (None = blank or foreign bytes) + records."""

    header: dict[str, int] | None
    records: tuple[dict[str, int], ...]   # non-blank slots up to the head, in order
    tail_clean: bool                      # nothing written after the first blank slot
    blank: bool                           # the whole page reads erased (all 0xFF)
    pad_clean: bool                       # header pad bytes all HEADER_PAD_FILL
                                          # (vacuously True without a valid header)


@dataclass(frozen=True)
class RegionView:
    """The whole 4 KB region: both pages, plus which one is current (highest seq)."""

    pages: tuple[PageView, PageView]
    current: int | None


def slice_nv(dump: bytes) -> bytes:
    """The 4 KB NV region out of a full 256 KB flash_dump image."""
    if len(dump) != DUMP_SIZE:
        raise ValueError(f"expected a {DUMP_SIZE}-byte dump, got {len(dump)}")
    return dump[spec.DUMP_OFFSET : spec.DUMP_OFFSET + spec.REGION_SIZE]


def parse_header(page: bytes) -> dict[str, int] | None:
    """The page header as a field dict, or None if blank/foreign.

    Validity mirrors the firmware's header_valid(): version + zero reserved0 +
    a plausible page_seq. (A leftover of the old proof demo -- one small counter
    doubleword -- yields page_seq == 0 and is correctly rejected.)
    """
    raw = page[: spec.HEADER_SIZE]
    if raw == spec.BLANK_HEADER:
        return None
    fields = dict(zip(spec.HEADER_FIELDS, struct.unpack(spec.HEADER_FMT, raw)))
    if fields["version"] != spec.SPEC_VERSION or fields["reserved0"] != 0:
        return None
    if not (1 <= fields["page_seq"] < 0xFFFFFFFF):
        return None
    return fields


def parse_page(page: bytes) -> PageView:
    """Parse one 2 KB page: records run from the header to the first blank slot."""
    if len(page) != spec.PAGE_SIZE:
        raise ValueError(f"expected a {spec.PAGE_SIZE}-byte page, got {len(page)}")
    records: list[dict[str, int]] = []
    head_seen = False
    tail_clean = True
    for i in range(spec.RECORDS_PER_PAGE):
        off = spec.HEADER_SIZE + i * spec.RECORD_SIZE
        raw = page[off : off + spec.RECORD_SIZE]
        if raw == spec.BLANK_RECORD:
            head_seen = True
        elif head_seen:
            tail_clean = False   # a written slot AFTER the head: the logger never does this
        else:
            records.append(dict(zip(spec.RECORD_FIELDS, struct.unpack(spec.RECORD_FMT, raw))))
    header = parse_header(page)
    # struct's "x" skips the pad on unpack, so surface its state as a separate
    # fact: the firmware programs it as HEADER_PAD_FILL, and anything else means
    # a rewritten/foreign header. Reported, never judged -- the training gate
    # (model/dataset.py) decides; the eval injector needs this parser neutral.
    pad = page[spec.HEADER_SIZE - spec.HEADER_PAD : spec.HEADER_SIZE]
    pad_clean = header is None or pad == bytes([spec.HEADER_PAD_FILL]) * spec.HEADER_PAD
    return PageView(header, tuple(records), tail_clean,
                    page == b"\xff" * spec.PAGE_SIZE, pad_clean)


def parse_region(nv: bytes) -> RegionView:
    """Parse the 4 KB region; current = the valid page with the highest page_seq."""
    if len(nv) != spec.REGION_SIZE:
        raise ValueError(f"expected a {spec.REGION_SIZE}-byte region, got {len(nv)}")
    pages = tuple(parse_page(nv[i * spec.PAGE_SIZE : (i + 1) * spec.PAGE_SIZE])
                  for i in range(spec.NUM_PAGES))
    current: int | None = None
    for i, p in enumerate(pages):
        if p.header is not None:
            if current is None or p.header["page_seq"] > pages[current].header["page_seq"]:
                current = i
    return RegionView((pages[0], pages[1]), current)


def records_chronological(view: RegionView) -> tuple[dict[str, int], ...]:
    """All records oldest-first: the lower-seq valid page's, then the current page's."""
    if view.current is None:
        return ()
    other = 1 - view.current
    older = view.pages[other].records if view.pages[other].header is not None else ()
    return tuple(older) + view.pages[view.current].records


# ---- CLI: human summary of one capture ------------------------------------------


def _page_line(i: int, p: PageView) -> str:
    base = spec.PAGE_BASES[i]
    if p.header is None:
        state = "blank" if p.blank else "FOREIGN (bytes present, no valid header)"
        return f"page{i} @0x{base:08X}: {state}"
    h = p.header
    line = (f"page{i} @0x{base:08X}: seq={h['page_seq']} boot={h['boot_count']} "
            f"op={h['op_count']} used={len(p.records)}/{spec.RECORDS_PER_PAGE} "
            f"tail={'clean' if p.tail_clean else 'DIRTY'}")
    stats = "  ".join(
        f"{ch.name}[{h[f'{ch.name}_min']}..{h[f'{ch.name}_max']} mean={h[f'{ch.name}_mean']}]"
        for ch in spec.CHANNELS)
    return f"{line}\n        {stats}"


def summarize(nv: bytes) -> str:
    """A multi-line human summary: pages, head, record count, ts + value ranges."""
    view = parse_region(nv)
    lines = [_page_line(i, p) for i, p in enumerate(view.pages)]
    recs = records_chronological(view)
    if view.current is None:
        lines.append("current=none  records=0")
        return "\n".join(lines)
    lines.append(f"current=page{view.current}  records(chronological)={len(recs)}")
    if recs:
        ts = [r["ts"] for r in recs]
        lines.append(f"ts: first={ts[0]} last={ts[-1]} "
                     f"(resets at reboot seams are benign)")
        for ch in spec.CHANNELS:
            vals = [r[ch.name] for r in recs]
            lo, hi = min(vals), max(vals)
            ok = "OK" if (lo >= ch.lo and hi <= ch.hi) else "OUT OF RANGE"
            lines.append(f"{ch.name}: {lo}..{hi} (legal {ch.lo}..{ch.hi}) {ok}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """Summarize one .bin (a 256 KB flash_dump capture, or a bare 4 KB NV slice)."""
    if len(argv) != 1:
        print("usage: python -m offdevice.nv.parse <capture.bin>")
        return 2
    data = Path(argv[0]).read_bytes()
    nv = data if len(data) == spec.REGION_SIZE else slice_nv(data)
    print(summarize(nv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
