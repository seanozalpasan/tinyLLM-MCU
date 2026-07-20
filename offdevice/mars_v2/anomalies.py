"""
    This Synthetically creates anomalous 4 KB NV regions
        1.  out_of_range takes a magnitude in LSB down to +1 past the boundary
        2.  journal_tamper covers op_count-backwards (what the real
            steady1_run003 anomaly actually is), a chain-end mimic, and illegal
            unit enums 
        3.  blobs vary placement (records at any slot / journal / header-spanning
            / non-current page), size, and content class (uniform-random,
            code-like bytes, ASCII text)
        4.  header (full smash, 1-byte version flip, stat rewrite, page_seq swap)
            and rollback (truncate K records, page erase, region erase) exist as
            first-class types 

        spec_dict: {"type": <SYNTH_TYPES key>, **params}; every parameter has a
        deterministic default drawn from rng, and the exact values used are
        returned so a battery manifest can record them.

    Run with:
        python -m offdevice.cnn_quant.mars_v2.anomalies --per-type 30
"""
from __future__ import annotations

import random
import struct

from offdevice.nv import spec
from offdevice.nv.parse import journal_chain, parse_region

DUMP_SIZE = spec.DUMP_OFFSET + spec.REGION_SIZE
# field byte offsets inside a 16 B record: ts@0, temp@4, hum@8, press@12
_F = {"ts": (0, "<I"), "temp": (4, "<i"), "hum": (8, "<I"), "press": (12, "<I")}

SYNTH_TYPES = ("out_of_range", "nonmonotonic_ts", "ts_seam_mimic",
               "correlation_break", "blob", "stride_break", "journal_tamper",
               "bitflip", "header", "rollback")

_TEXT_FILL = (b"[boot] BME280 init OK\r\n[log] t=23.41C rh=40.2%% p=1013.2hPa\r\n"
              b"[log] journal J1 unit_temp=1\r\n[wd] kick ok\r\n")

# Thumb-like halfword stream for the no-aux code-content fallback: realistic
# instruction-byte statistics (push/ldr/str/mov/bx patterns), entropy well below
# uniform-random.
_CODE_OPS = (0xB580, 0x4B08, 0x681B, 0x2B00, 0xD005, 0x4A06, 0x6011, 0x3301,
             0x4618, 0xBD80, 0x4770, 0xB510, 0x6843, 0x4C05, 0x60E0, 0xE7F6)


def _current_page_off(nv: bytes) -> int:
    """Byte offset of the current (highest-seq) page within the 4 KB region."""
    view = parse_region(nv)
    idx = view.current if view.current is not None else 0
    return idx * spec.PAGE_SIZE


def _record_off(nv: bytes, k: int) -> int:
    """Byte offset of record slot k in the current page."""
    return _current_page_off(nv) + spec.RECORDS_OFFSET + k * spec.RECORD_SIZE


def _n_records(nv: bytes) -> int:
    view = parse_region(nv)
    if view.current is None:
        return 0
    return len(view.pages[view.current].records)


def _read_field(nv: bytes, rec_off: int, field: str) -> int:
    off, fmt = _F[field]
    return struct.unpack_from(fmt, nv, rec_off + off)[0]


def _write_field(buf: bytearray, rec_off: int, field: str, val: int):
    off, fmt = _F[field]
    struct.pack_into(fmt, buf, rec_off + off, val)


def _code_bytes(size: int, rng: random.Random, aux: bytes | None) -> bytes:
    if aux is not None and len(aux) >= size + 64:
        start = rng.randrange(0, len(aux) - size)
        return aux[start:start + size]
    out = bytearray()
    while len(out) < size:
        out += struct.pack("<H", _CODE_OPS[rng.randrange(len(_CODE_OPS))]
                           ^ rng.randrange(8))
    return bytes(out[:size])


def _content(kind: str, size: int, rng: random.Random, aux: bytes | None) -> bytes:
    if kind == "random":
        return bytes(rng.randrange(256) for _ in range(size))
    if kind == "code":
        return _code_bytes(size, rng, aux)
    if kind == "text":
        reps = (size // len(_TEXT_FILL)) + 1
        return (_TEXT_FILL * reps)[:size]
    raise ValueError(f"unknown blob content {kind!r}")


def _journal_slots(nv: bytes, page_off: int) -> tuple[int, int]:
    """(n_chain_entries, first_blank_slot_index or JOURNAL_SLOTS if full)."""
    view = parse_region(nv)
    page = view.pages[page_off // spec.PAGE_SIZE]
    chain = journal_chain(page)
    return len(chain), len(chain)


def _pack_journal_entry(buf: bytearray, page_off: int, slot: int,
                        unit_temp: int, unit_press: int, reserved0: int,
                        op_count: int) -> None:
    off = page_off + spec.JOURNAL_OFFSET + slot * spec.JOURNAL_ENTRY_SIZE
    struct.pack_into(spec.JOURNAL_FMT, buf, off,
                     unit_temp, unit_press, reserved0, op_count)


def synthesize(nv: bytes, spec_dict: dict, rng: random.Random,
               code_src: bytes | None = None) -> tuple[bytes, dict] | None:
    """Apply one parameterized tamper; return (bytes, params_used) or None.

    spec_dict["type"] picks the SYNTH_TYPES entry; remaining keys override the
    per-type defaults. params_used echoes every parameter that shaped the bytes
    (for a manifest). code_src supplies realistic code-content bytes for blobs
    (e.g. the base capture's own firmware region).
    """
    atype = spec_dict["type"]
    p = dict(spec_dict)
    b = bytearray(nv)
    n = _n_records(nv)
    view = parse_region(nv)
    cur_off = _current_page_off(nv)

    if atype == "out_of_range":
        if n < 4:
            return None
        ch = {c.name: c for c in spec.CHANNELS}[p.get("channel") or
                                                rng.choice(spec.CHANNELS).name]
        mag = int(p.get("magnitude", 100))
        direction = p.get("direction") or rng.choice(("above", "below"))
        if direction == "below" and ch.name == "hum":
            return None               # hum lo == 0: a u32 cannot go below range
        val = ch.hi + mag if direction == "above" else ch.lo - mag
        k = p.get("slot")
        if k is None:
            k = rng.randrange(1, n - 1)
        _write_field(b, _record_off(nv, k), ch.name, val)
        p.update(channel=ch.name, magnitude=mag, direction=direction, slot=k)

    elif atype == "nonmonotonic_ts":
        if n < 8:
            return None
        delta = int(p.get("magnitude", 500))       # seconds subtracted
        run = int(p.get("run", 1))
        k0 = p.get("slot")
        if k0 is None:
            k0 = rng.randrange(2, max(3, n - run - 1))
        landed = []
        for k in range(k0, min(k0 + run, n)):
            ts = _read_field(nv, _record_off(nv, k), "ts")
            new = ts - delta
            if new <= 30:             # keep it a MID-STREAM landing, not a seam
                return None           # base too shallow for this magnitude here
            _write_field(b, _record_off(nv, k), "ts", new)
            landed.append(new)
        p.update(magnitude=delta, run=run, slot=k0, landings=landed)

    elif atype == "ts_seam_mimic":
        # adversarial DESIGNED MISS: replay a full boot-reset seam mid-stream --
        # ts restarts at 15 and walks 15, 30, 45... to the page end, exactly the
        # byte pattern real reboots leave. Channel values stay smooth
        # (environments don't reboot). No spec invariant can separate this from
        # a real reset without cross-snapshot state.
        if n < 12:
            return None
        k0 = p.get("slot")
        if k0 is None:
            k0 = rng.randrange(4, n - 4)
        for i, k in enumerate(range(k0, n)):
            _write_field(b, _record_off(nv, k), "ts",
                         spec.RATE_DEPLOY_PERIOD_S * (i + 1))
        p.update(slot=k0, rewritten=n - k0)

    elif atype == "correlation_break":
        if n < 8:
            return None
        mag = int(p.get("magnitude", 1))
        run = int(p.get("run", 13))
        sign = int(p.get("sign") or rng.choice((1, -1)))
        k0 = p.get("slot")
        if k0 is None:
            k0 = rng.randrange(1, max(2, n - run))
        temp = {c.name: c for c in spec.CHANNELS}["temp"]
        for k in range(k0, min(k0 + run, n)):
            off = _record_off(nv, k)
            t = _read_field(nv, off, "temp") + sign * mag
            _write_field(b, off, "temp", max(temp.lo, min(temp.hi, t)))
        p.update(magnitude=mag, run=run, sign=sign, slot=k0)

    elif atype == "blob":
        size = int(p.get("size", 512))
        kind = p.get("content") or rng.choice(("random", "code", "text"))
        placement = p.get("placement") or rng.choice(
            ("records", "journal", "header", "nonpage"))
        if placement == "records":
            if n < 4:
                return None
            slot = p.get("slot")
            if slot is None:
                slot = rng.randrange(0, max(1, n - 1))   # varied, not always 1
            start = _record_off(nv, slot)
        elif placement == "journal":
            start = cur_off + spec.JOURNAL_OFFSET
        elif placement == "header":
            start = cur_off                      # spans header (+journal beyond 64 B)
        elif placement == "nonpage":
            other = spec.PAGE_SIZE - cur_off if spec.NUM_PAGES == 2 else 0
            start = other + rng.randrange(0, spec.PAGE_SIZE // 2)
        else:
            raise ValueError(f"unknown placement {placement!r}")
        size = min(size, len(b) - start)
        b[start:start + size] = _content(kind, size, rng, code_src)
        p.update(size=size, content=kind, placement=placement, offset=start)

    elif atype == "stride_break":
        if n < 8:
            return None
        shift = int(p.get("magnitude", 3))
        k0 = p.get("slot")
        if k0 is None:
            k0 = rng.randrange(1, n - 2)
        start = _record_off(nv, k0)
        end = cur_off + spec.PAGE_SIZE
        b[start + shift:end] = nv[start:end - shift]
        p.update(magnitude=shift, slot=k0)

    elif atype == "journal_tamper":
        mode = p.get("mode") or rng.choice(
            ("reserved0", "op_backwards", "chain_end_mimic", "unit_bad"))
        n_chain, first_blank = _journal_slots(nv, cur_off)
        if n_chain == 0:
            return None
        chain = journal_chain(view.pages[cur_off // spec.PAGE_SIZE])
        last_op = chain[-1]["op_count"]
        if mode == "reserved0":
            joff = cur_off + spec.JOURNAL_OFFSET
            struct.pack_into("<H", b, joff + 2, 0xBEEF)
        elif mode in ("op_backwards", "chain_end_mimic", "unit_bad"):
            if first_blank >= spec.JOURNAL_SLOTS:
                slot = spec.JOURNAL_SLOTS - 1     # chain full: overwrite the end
            else:
                slot = first_blank
            if mode == "op_backwards":
                # a chain entry whose op_count steps BACKWARDS (replayed older
                # settings state)
                op = max(0, last_op - rng.randint(3, 60))
                _pack_journal_entry(b, cur_off, slot, chain[-1]["unit_temp"],
                                    chain[-1]["unit_press"], 0, op)
                p.update(op_count=op, prev_op=last_op)
            elif mode == "chain_end_mimic":
                # DESIGNED MISS: a fully legal-looking settings change (equal
                # op_count is benign; units flip within {0,1}; reserved0 == 0)
                _pack_journal_entry(b, cur_off, slot,
                                    1 - chain[-1]["unit_temp"],
                                    chain[-1]["unit_press"], 0, last_op)
                p.update(op_count=last_op)
            else:
                _pack_journal_entry(b, cur_off, slot, 7, 0, 0, last_op)
            p.update(slot=slot)
        else:
            raise ValueError(f"unknown journal mode {mode!r}")
        p.update(mode=mode)

    elif atype == "bitflip":
        nbits = int(p.get("magnitude", 1))
        region = p.get("region") or rng.choice(("records", "header", "journal"))
        if region == "records":
            if n < 2:
                return None
            lo, hi = _record_off(nv, 0), _record_off(nv, n)
        elif region == "header":
            lo, hi = cur_off, cur_off + spec.HEADER_SIZE
        else:
            lo, hi = (cur_off + spec.JOURNAL_OFFSET,
                      cur_off + spec.JOURNAL_OFFSET + spec.JOURNAL_SIZE)
        flips = []
        for _ in range(nbits):
            pos = rng.randrange(lo, hi)
            bit = rng.randrange(8)
            b[pos] ^= (1 << bit)
            flips.append([pos, bit])
        p.update(magnitude=nbits, region=region, flips=flips)

    elif atype == "header":
        mode = p.get("mode") or rng.choice(
            ("full_smash", "version_flip", "stat_rewrite", "page_seq_swap"))
        if view.pages[cur_off // spec.PAGE_SIZE].header is None:
            return None
        if mode == "full_smash":
            b[cur_off:cur_off + spec.HEADER_SIZE] = _content(
                "random", spec.HEADER_SIZE, rng, None)
        elif mode == "version_flip":
            # a 1-byte flip: header fails validation, the page's records go
            # silently invisible to a chronological parse
            ver = struct.unpack_from("<H", nv, cur_off)[0]
            struct.pack_into("<H", b, cur_off, ver + 1)
        elif mode == "stat_rewrite":
            # make temp stats impossible: mean above max
            hdr_fields = dict(zip(spec.HEADER_FIELDS,
                                  struct.unpack(spec.HEADER_FMT,
                                                nv[cur_off:cur_off + spec.HEADER_SIZE])))
            off = struct.calcsize("<HHIII") + 2 * 4   # temp_mean: after 5 head fields + min,max
            struct.pack_into("<i", b, cur_off + off, hdr_fields["temp_max"] + 500)
        elif mode == "page_seq_swap":
            other_off = spec.PAGE_SIZE - cur_off
            if view.pages[other_off // spec.PAGE_SIZE].header is None:
                return None
            s_cur = struct.unpack_from("<I", nv, cur_off + 4)[0]
            s_oth = struct.unpack_from("<I", nv, other_off + 4)[0]
            struct.pack_into("<I", b, cur_off + 4, s_oth)
            struct.pack_into("<I", b, other_off + 4, s_cur)
        else:
            raise ValueError(f"unknown header mode {mode!r}")
        p.update(mode=mode)

    elif atype == "rollback":
        mode = p.get("mode") or "truncate"
        if mode == "truncate":
            k = int(p.get("magnitude", 8))       # records erased off the tail
            if n < k + 2:
                return None
            start = _record_off(nv, n - k)
            end = _record_off(nv, n)
            b[start:end] = bytes([spec.ERASED_BYTE]) * (end - start)
            p.update(magnitude=k)
        elif mode == "page_erase":
            b[cur_off:cur_off + spec.PAGE_SIZE] = (bytes([spec.ERASED_BYTE])
                                                   * spec.PAGE_SIZE)
        elif mode == "region_erase":
            b[:] = bytes([spec.ERASED_BYTE]) * len(b)
        else:
            raise ValueError(f"unknown rollback mode {mode!r}")
        p.update(mode=mode)

    else:
        raise ValueError(f"unknown synth type {atype!r}")

    return bytes(b), p
