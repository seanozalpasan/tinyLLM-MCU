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

    Used by the sandbox generator:
        python -m offdevice.mars_v2.sandbox --seed <n>
"""
from __future__ import annotations

import random
import struct

from offdevice.nv import spec
from offdevice.nv.parse import journal_chain, parse_region

DUMP_SIZE = spec.DUMP_OFFSET + spec.REGION_SIZE
# field byte offsets inside a 16 B record: ts@0, temp@4, hum@8, press@12
_FIELD_OFFSETS = {"ts": (0, "<I"), "temp": (4, "<i"),
                  "hum": (8, "<I"), "press": (12, "<I")}

SYNTH_TYPES = ("out_of_range", "nonmonotonic_ts", "ts_seam_mimic",
               "correlation_break", "blob", "stride_break", "journal_tamper",
               "bitflip", "header", "rollback")

_TEXT_FILL = (b"[boot] BME280 init OK\r\n[log] t=23.41C rh=40.2%% p=1013.2hPa\r\n"
              b"[log] journal J1 unit_temp=1\r\n[wd] kick ok\r\n")

# Thumb-like halfword stream for blob content when no real firmware bytes are
# handed in: realistic instruction-byte statistics (push/ldr/str/mov/bx
# patterns), entropy well below uniform-random.
_CODE_OPS = (0xB580, 0x4B08, 0x681B, 0x2B00, 0xD005, 0x4A06, 0x6011, 0x3301,
             0x4618, 0xBD80, 0x4770, 0xB510, 0x6843, 0x4C05, 0x60E0, 0xE7F6)


def _current_page_off(nv: bytes) -> int:
    """Byte offset of the current (highest-seq) page within the 4 KB region."""
    view = parse_region(nv)
    page_index = view.current if view.current is not None else 0
    return page_index * spec.PAGE_SIZE


def _record_off(nv: bytes, slot: int) -> int:
    """Byte offset of record slot `slot` in the current page."""
    return _current_page_off(nv) + spec.RECORDS_OFFSET + slot * spec.RECORD_SIZE


def _n_records(nv: bytes) -> int:
    view = parse_region(nv)
    if view.current is None:
        return 0
    return len(view.pages[view.current].records)


def _read_field(nv: bytes, record_offset: int, field: str) -> int:
    field_offset, fmt = _FIELD_OFFSETS[field]
    return struct.unpack_from(fmt, nv, record_offset + field_offset)[0]


def _write_field(buffer: bytearray, record_offset: int, field: str, value: int):
    field_offset, fmt = _FIELD_OFFSETS[field]
    struct.pack_into(fmt, buffer, record_offset + field_offset, value)


def _code_bytes(size: int, rng: random.Random, source: bytes | None) -> bytes:
    if source is not None and len(source) >= size + 64:
        start = rng.randrange(0, len(source) - size)
        return source[start:start + size]
    result = bytearray()
    while len(result) < size:
        result += struct.pack("<H", _CODE_OPS[rng.randrange(len(_CODE_OPS))]
                              ^ rng.randrange(8))
    return bytes(result[:size])


def _content(kind: str, size: int, rng: random.Random,
             source: bytes | None) -> bytes:
    if kind == "random":
        return bytes(rng.randrange(256) for _ in range(size))
    if kind == "code":
        return _code_bytes(size, rng, source)
    if kind == "text":
        repeats = (size // len(_TEXT_FILL)) + 1
        return (_TEXT_FILL * repeats)[:size]
    raise ValueError(f"unknown blob content {kind!r}")


def _journal_slots(nv: bytes, page_offset: int) -> tuple[int, int]:
    """(chain_length, first_blank_slot_index or JOURNAL_SLOTS if full)."""
    view = parse_region(nv)
    page = view.pages[page_offset // spec.PAGE_SIZE]
    chain = journal_chain(page)
    return len(chain), len(chain)


def _pack_journal_entry(buffer: bytearray, page_offset: int, slot: int,
                        unit_temp: int, unit_press: int, reserved0: int,
                        op_count: int) -> None:
    entry_offset = page_offset + spec.JOURNAL_OFFSET + slot * spec.JOURNAL_ENTRY_SIZE
    struct.pack_into(spec.JOURNAL_FMT, buffer, entry_offset,
                     unit_temp, unit_press, reserved0, op_count)


def synthesize(nv: bytes, spec_dict: dict, rng: random.Random,
               code_src: bytes | None = None) -> tuple[bytes, dict] | None:
    """Apply one parameterized tamper; return (bytes, params_used) or None.

    spec_dict["type"] picks the SYNTH_TYPES entry; remaining keys override the
    per-type defaults. params_used echoes every parameter that shaped the bytes
    (for a manifest). code_src supplies realistic code-content bytes for blobs
    (e.g. the base capture's own firmware region).
    """
    anomaly_type = spec_dict["type"]
    params = dict(spec_dict)
    buffer = bytearray(nv)
    record_count = _n_records(nv)
    view = parse_region(nv)
    current_page_offset = _current_page_off(nv)

    if anomaly_type == "out_of_range":
        if record_count < 4:
            return None
        channel = {c.name: c for c in spec.CHANNELS}[params.get("channel") or
                                                     rng.choice(spec.CHANNELS).name]
        magnitude = int(params.get("magnitude", 100))
        direction = params.get("direction") or rng.choice(("above", "below"))
        if direction == "below" and channel.name == "hum":
            return None               # hum lo == 0: a u32 cannot go below range
        value = channel.hi + magnitude if direction == "above" else channel.lo - magnitude
        slot = params.get("slot")
        if slot is None:
            slot = rng.randrange(1, record_count - 1)
        _write_field(buffer, _record_off(nv, slot), channel.name, value)
        params.update(channel=channel.name, magnitude=magnitude,
                      direction=direction, slot=slot)

    elif anomaly_type == "nonmonotonic_ts":
        if record_count < 8:
            return None
        magnitude = int(params.get("magnitude", 500))    # seconds subtracted
        run = int(params.get("run", 1))
        start_slot = params.get("slot")
        if start_slot is None:
            start_slot = rng.randrange(2, max(3, record_count - run - 1))
        landings = []
        for slot in range(start_slot, min(start_slot + run, record_count)):
            timestamp = _read_field(nv, _record_off(nv, slot), "ts")
            new_timestamp = timestamp - magnitude
            if new_timestamp <= 30:   # keep it a MID-STREAM landing, not a reboot seam
                return None           # base too shallow for this magnitude here
            _write_field(buffer, _record_off(nv, slot), "ts", new_timestamp)
            landings.append(new_timestamp)
        params.update(magnitude=magnitude, run=run, slot=start_slot,
                      landings=landings)

    elif anomaly_type == "ts_seam_mimic":
        # intentionally VERY hard to catch: replays a full reboot seam
        # mid-stream -- ts restarts at 15 and walks 15, 30, 45... to the page
        # end, exactly the byte pattern real reboots leave. Channel values stay
        # smooth (environments don't reboot). Nothing in the spec alone can
        # separate this from a real reset without comparing snapshots over time.
        if record_count < 12:
            return None
        start_slot = params.get("slot")
        if start_slot is None:
            start_slot = rng.randrange(4, record_count - 4)
        for step, slot in enumerate(range(start_slot, record_count)):
            _write_field(buffer, _record_off(nv, slot), "ts",
                         spec.RATE_DEPLOY_PERIOD_S * (step + 1))
        params.update(slot=start_slot, rewritten=record_count - start_slot)

    elif anomaly_type == "correlation_break":
        if record_count < 8:
            return None
        magnitude = int(params.get("magnitude", 1))
        run = int(params.get("run", 13))
        sign = int(params.get("sign") or rng.choice((1, -1)))
        start_slot = params.get("slot")
        if start_slot is None:
            start_slot = rng.randrange(1, max(2, record_count - run))
        temp_channel = {c.name: c for c in spec.CHANNELS}["temp"]
        for slot in range(start_slot, min(start_slot + run, record_count)):
            record_offset = _record_off(nv, slot)
            nudged = _read_field(nv, record_offset, "temp") + sign * magnitude
            _write_field(buffer, record_offset, "temp",
                         max(temp_channel.lo, min(temp_channel.hi, nudged)))
        params.update(magnitude=magnitude, run=run, sign=sign, slot=start_slot)

    elif anomaly_type == "blob":
        size = int(params.get("size", 512))
        kind = params.get("content") or rng.choice(("random", "code", "text"))
        placement = params.get("placement") or rng.choice(
            ("records", "journal", "header", "nonpage"))
        if placement == "records":
            if record_count < 4:
                return None
            slot = params.get("slot")
            if slot is None:
                slot = rng.randrange(0, max(1, record_count - 1))   # varied slot
            start = _record_off(nv, slot)
        elif placement == "journal":
            start = current_page_offset + spec.JOURNAL_OFFSET
        elif placement == "header":
            start = current_page_offset    # spans header (+journal beyond 64 B)
        elif placement == "nonpage":
            other_page = (spec.PAGE_SIZE - current_page_offset
                          if spec.NUM_PAGES == 2 else 0)
            start = other_page + rng.randrange(0, spec.PAGE_SIZE // 2)
        else:
            raise ValueError(f"unknown placement {placement!r}")
        size = min(size, len(buffer) - start)
        buffer[start:start + size] = _content(kind, size, rng, code_src)
        params.update(size=size, content=kind, placement=placement, offset=start)

    elif anomaly_type == "stride_break":
        if record_count < 8:
            return None
        shift = int(params.get("magnitude", 3))
        start_slot = params.get("slot")
        if start_slot is None:
            start_slot = rng.randrange(1, record_count - 2)
        start = _record_off(nv, start_slot)
        end = current_page_offset + spec.PAGE_SIZE
        buffer[start + shift:end] = nv[start:end - shift]
        params.update(magnitude=shift, slot=start_slot)

    elif anomaly_type == "journal_tamper":
        mode = params.get("mode") or rng.choice(
            ("reserved0", "op_backwards", "chain_end_mimic", "unit_bad"))
        chain_length, first_blank = _journal_slots(nv, current_page_offset)
        if chain_length == 0:
            return None
        chain = journal_chain(view.pages[current_page_offset // spec.PAGE_SIZE])
        last_op_count = chain[-1]["op_count"]
        if mode == "reserved0":
            journal_offset = current_page_offset + spec.JOURNAL_OFFSET
            struct.pack_into("<H", buffer, journal_offset + 2, 0xBEEF)
        elif mode in ("op_backwards", "chain_end_mimic", "unit_bad"):
            if first_blank >= spec.JOURNAL_SLOTS:
                slot = spec.JOURNAL_SLOTS - 1     # chain full: overwrite the end
            else:
                slot = first_blank
            if mode == "op_backwards":
                # a chain entry whose op_count steps BACKWARDS (a replayed
                # older settings state -- what the real steady1_run003 anomaly is)
                new_op_count = max(0, last_op_count - rng.randint(3, 60))
                _pack_journal_entry(buffer, current_page_offset, slot,
                                    chain[-1]["unit_temp"],
                                    chain[-1]["unit_press"], 0, new_op_count)
                params.update(op_count=new_op_count, prev_op=last_op_count)
            elif mode == "chain_end_mimic":
                # intentionally hard to catch: a fully legal-looking settings
                # change (equal op_count is benign; units flip within {0,1};
                # reserved0 == 0)
                _pack_journal_entry(buffer, current_page_offset, slot,
                                    1 - chain[-1]["unit_temp"],
                                    chain[-1]["unit_press"], 0, last_op_count)
                params.update(op_count=last_op_count)
            else:
                _pack_journal_entry(buffer, current_page_offset, slot,
                                    7, 0, 0, last_op_count)
            params.update(slot=slot)
        else:
            raise ValueError(f"unknown journal mode {mode!r}")
        params.update(mode=mode)

    elif anomaly_type == "bitflip":
        flip_count = int(params.get("magnitude", 1))
        region = params.get("region") or rng.choice(("records", "header", "journal"))
        if region == "records":
            if record_count < 2:
                return None
            range_start = _record_off(nv, 0)
            range_end = _record_off(nv, record_count)
        elif region == "header":
            range_start = current_page_offset
            range_end = current_page_offset + spec.HEADER_SIZE
        else:
            range_start = current_page_offset + spec.JOURNAL_OFFSET
            range_end = range_start + spec.JOURNAL_SIZE
        flips = []
        for _ in range(flip_count):
            byte_position = rng.randrange(range_start, range_end)
            bit_position = rng.randrange(8)
            buffer[byte_position] ^= (1 << bit_position)
            flips.append([byte_position, bit_position])
        params.update(magnitude=flip_count, region=region, flips=flips)

    elif anomaly_type == "header":
        mode = params.get("mode") or rng.choice(
            ("full_smash", "version_flip", "stat_rewrite", "page_seq_swap"))
        if view.pages[current_page_offset // spec.PAGE_SIZE].header is None:
            return None
        if mode == "full_smash":
            buffer[current_page_offset:current_page_offset + spec.HEADER_SIZE] = \
                _content("random", spec.HEADER_SIZE, rng, None)
        elif mode == "version_flip":
            # a 1-byte flip: the header fails validation and the page's records
            # go silently invisible to a chronological parse
            version = struct.unpack_from("<H", nv, current_page_offset)[0]
            struct.pack_into("<H", buffer, current_page_offset, version + 1)
        elif mode == "stat_rewrite":
            # make the temp stats impossible: mean above max
            header_fields = dict(zip(
                spec.HEADER_FIELDS,
                struct.unpack(spec.HEADER_FMT,
                              nv[current_page_offset:
                                 current_page_offset + spec.HEADER_SIZE])))
            # temp_mean sits after the 5 head fields plus temp min and max
            mean_offset = struct.calcsize("<HHIII") + 2 * 4
            struct.pack_into("<i", buffer, current_page_offset + mean_offset,
                             header_fields["temp_max"] + 500)
        elif mode == "page_seq_swap":
            other_page_offset = spec.PAGE_SIZE - current_page_offset
            if view.pages[other_page_offset // spec.PAGE_SIZE].header is None:
                return None
            current_seq = struct.unpack_from("<I", nv, current_page_offset + 4)[0]
            other_seq = struct.unpack_from("<I", nv, other_page_offset + 4)[0]
            struct.pack_into("<I", buffer, current_page_offset + 4, other_seq)
            struct.pack_into("<I", buffer, other_page_offset + 4, current_seq)
        else:
            raise ValueError(f"unknown header mode {mode!r}")
        params.update(mode=mode)

    elif anomaly_type == "rollback":
        mode = params.get("mode") or "truncate"
        if mode == "truncate":
            erase_count = int(params.get("magnitude", 8))   # records erased off the tail
            if record_count < erase_count + 2:
                return None
            start = _record_off(nv, record_count - erase_count)
            end = _record_off(nv, record_count)
            buffer[start:end] = bytes([spec.ERASED_BYTE]) * (end - start)
            params.update(magnitude=erase_count)
        elif mode == "page_erase":
            buffer[current_page_offset:current_page_offset + spec.PAGE_SIZE] = \
                bytes([spec.ERASED_BYTE]) * spec.PAGE_SIZE
        elif mode == "region_erase":
            buffer[:] = bytes([spec.ERASED_BYTE]) * len(buffer)
        else:
            raise ValueError(f"unknown rollback mode {mode!r}")
        params.update(mode=mode)

    else:
        raise ValueError(f"unknown synth type {anomaly_type!r}")

    return bytes(buffer), params
