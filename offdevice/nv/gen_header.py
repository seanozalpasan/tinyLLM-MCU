"""
Render nv_spec.h from spec.py into both firmware projects (Secure + NonSecure).

Run from the repo root after ANY spec.py change:

    python -m offdevice.nv.gen_header

Both copies are byte-identical by construction. A new header needs no .project
surgery (CubeIDE finds it via the existing include paths -- unlike .c files),
and test_nv_spec.py fails loudly if the on-disk copies drift from spec.py.
"""

import struct
from pathlib import Path

from offdevice.nv import spec

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGETS = tuple(
    REPO_ROOT / "firmware" / "memAcq" / project / "Core" / "Inc" / "nv_spec.h"
    for project in ("Secure", "NonSecure")
)

_NAME_W = 28   # #define name column
_LINE_W = 50   # column where a #define's trailing comment starts
_DECL_W = 20   # column where a struct field's trailing comment starts


def _define(name: str, value: str, comment: str = "") -> str:
    """One aligned `#define` line, with an optional trailing comment."""
    line = f"#define {name:<{_NAME_W}}{value}"
    if comment:
        line = f"{line:<{_LINE_W}}/* {comment} */"
    return line


def _field(c_type: str, name: str, comment: str = "") -> str:
    """One aligned struct-field line, with an optional trailing comment."""
    decl = f"  {c_type:<9}{name};"
    if comment:
        decl = f"{decl:<{_DECL_W}}/* {comment} */"
    return decl


def _c_int(value: int, signed: bool) -> str:
    """An integer as a C literal; negatives parenthesized for macro safety."""
    if signed:
        return f"({value}L)" if value < 0 else f"{value}L"
    return f"{value}UL"


def _lock(tag: str, cond: str) -> str:
    """One aligned NV_LAYOUT_LOCK line (the editor-safe static assert)."""
    return f"NV_LAYOUT_LOCK({tag + ',':<23}{cond});"


def render() -> str:
    """The full nv_spec.h text, deterministically derived from spec.py."""
    off_page_seq = struct.calcsize("<HH")
    off_first_stat = struct.calcsize("<HHIII")
    off_record_ch0 = struct.calcsize("<I")
    first_stat = f"{spec.CHANNELS[0].name}_{spec.HEADER_STATS[0]}"
    ch0 = spec.CHANNELS[0].name
    layout = (f"[NvHeader {spec.HEADER_SIZE} B]"
              + f"[{spec.JOURNAL_SLOTS} x NvJournalEntry {spec.JOURNAL_ENTRY_SIZE} B]"
              + f"[{spec.RECORDS_PER_PAGE} x NvRecord {spec.RECORD_SIZE} B]")

    lines = [
        "/*",
        " * nv_spec.h -- NV-region byte spec. GENERATED from offdevice/nv/spec.py -- DO NOT EDIT.",
        " *",
        " * Regenerate (repo root):  python -m offdevice.nv.gen_header",
        " *",
        " * The NonSecure logger writes this layout; the Python reader parses it",
        " * byte-for-byte. A hand edit here drifts the two apart -- edit spec.py.",
        " * Layout per 2 KB page (little-endian == the M33's native order):",
        " *",
        f" *     {layout}",
        " *",
        " * The header is programmed once when a page is erased+reopened; records then",
        " * append into fixed slots (two 8 B flash doublewords each -- nothing in flash",
        " * is ever rewritten in place). An all-0xFF slot is blank. Newest page =",
        " * highest page_seq; the write head is the first blank slot of that page.",
        " * The settings journal sits between header and records: page-open stamps J0",
        " * from RAM, each runtime change programs the next blank slot, and the live",
        " * setting is the end of the contiguous chain (found at boot, never stored).",
        " * Records stay canonical no matter what the settings say.",
        " */",
        "#ifndef NV_SPEC_H",
        "#define NV_SPEC_H",
        "",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        "/* ===== region geometry (mirrors the locked TrustZone partition) ===== */",
        _define("NV_NS_FLASH_BASE", f"0x{spec.NS_FLASH_BASE:08X}UL", "NS Bank-2 base == flash_dump origin"),
        _define("NV_REGION_BASE", f"0x{spec.REGION_BASE:08X}UL", "top two 2 KB pages (126/127)"),
        _define("NV_REGION_SIZE", f"0x{spec.REGION_SIZE:X}UL"),
        _define("NV_PAGE_SIZE", f"0x{spec.PAGE_SIZE:X}UL"),
        _define("NV_NUM_PAGES", f"{spec.NUM_PAGES}U"),
        _define("NV_PAGE0_BASE", f"0x{spec.PAGE_BASES[0]:08X}UL"),
        _define("NV_PAGE1_BASE", f"0x{spec.PAGE_BASES[1]:08X}UL"),
        _define("NV_DUMP_OFFSET", f"0x{spec.DUMP_OFFSET:X}UL", "region offset inside a 256 KB dump"),
        _define("NV_STATIC_SIZE", f"0x{spec.STATIC_SIZE:X}UL", "Part-1 hash = [base, base + this)"),
        _define("NV_FLASH_DOUBLEWORD", f"{spec.DOUBLEWORD}U", "program granularity; strides are multiples"),
        _define("NV_ERASED_BYTE", f"0x{spec.ERASED_BYTE:02X}U", "un-programmed flash reads as this"),
        "",
        "/* ===== layout ===== */",
        _define("NV_SPEC_VERSION", f"{spec.SPEC_VERSION}U"),
        _define("NV_HEADER_SIZE", f"{spec.HEADER_SIZE}U"),
        _define("NV_HEADER_PAD_FILL", f"0x{spec.HEADER_PAD_FILL:02X}U", "trailing reserve programmed as zeros"),
        _define("NV_JOURNAL_OFFSET", f"0x{spec.JOURNAL_OFFSET:03X}U", "J0 sits directly after the header"),
        _define("NV_JOURNAL_SLOTS", f"{spec.JOURNAL_SLOTS}U"),
        _define("NV_JOURNAL_ENTRY_SIZE", f"{spec.JOURNAL_ENTRY_SIZE}U", "== one doubleword: atomic vs reset"),
        _define("NV_JOURNAL_SIZE", f"{spec.JOURNAL_SIZE}U"),
        _define("NV_RECORD_SIZE", f"{spec.RECORD_SIZE}U"),
        _define("NV_RECORDS_OFFSET", f"0x{spec.RECORDS_OFFSET:03X}U", "first record slot in a page"),
        _define("NV_RECORDS_PER_PAGE", f"{spec.RECORDS_PER_PAGE}U"),
        _define("NV_RECORDS_TOTAL", f"{spec.RECORDS_TOTAL}U"),
        "",
        "/* ===== value semantics: fixed-point integers, BME280 measurement ranges ===== */",
    ]

    for ch in spec.CHANNELS:
        signed = ch.fmt.islower()
        name = ch.name.upper()
        comment = f"{ch.c_type} = {ch.unit} x{ch.scale}"
        if ch.note:
            comment = f"{comment} {ch.note}"
        lines.append(_define(f"NV_{name}_SCALE", str(ch.scale), comment))
        lines.append(_define(f"NV_{name}_LO", _c_int(ch.lo, signed)))
        lines.append(_define(f"NV_{name}_HI", _c_int(ch.hi, signed)))

    lines += [
        "",
        "/* ===== display units (what telemetry says; records stay canonical) ===== */",
        _define("NV_UNIT_TEMP_C", f"{spec.UNIT_TEMP_C}U", "default; records always store degC x100"),
        _define("NV_UNIT_TEMP_F", f"{spec.UNIT_TEMP_F}U"),
        _define("NV_UNIT_PRESS_HPA", f"{spec.UNIT_PRESS_HPA}U", "default; records always store hPa x100"),
        _define("NV_UNIT_PRESS_INHG", f"{spec.UNIT_PRESS_INHG}U"),
        "",
        "/* ===== update-rate presets (period between records, seconds) ===== */",
        "/* Flash ages by erase count (~10k cycles/page rated): 1 s wraps the ring in",
        "   ~4 min for bring-up only; 15 s erases each page every ~61 min",
        "   => ~14 months to the rated minimum (deliberately traded down from 45 s",
        "   for campaign speed). Training data = deploy rate only. */",
        _define("NV_RATE_DEV_PERIOD_S", f"{spec.RATE_DEV_PERIOD_S}U"),
        _define("NV_RATE_DEPLOY_PERIOD_S", f"{spec.RATE_DEPLOY_PERIOD_S}U"),
        "",
        "/* Programmed once per page-open; counters + stats are RAM-kept and snapshotted",
        "   at that moment (flash cannot rewrite in place). page_seq: monotonic page-open",
        "   counter, 1 on virgin flash -- doubles as the wrap counter, and the highest",
        "   one marks the current page. boot_count: boots as of this page-open.",
        "   op_count: records fully programmed before this page opened (lifetime total",
        "   = op_count + the page's non-blank slots). Stats: per channel over all",
        "   readings THIS boot, including the one that triggered the page-open;",
        "   mean = 64-bit sum / count, truncated toward zero (C99 division). */",
        "typedef struct",
        "{",
        _field("uint16_t", "version"),
        _field("uint16_t", "reserved0"),
        _field("uint32_t", "page_seq"),
        _field("uint32_t", "boot_count"),
        _field("uint32_t", "op_count"),
    ]

    for ch in spec.CHANNELS:
        for stat in spec.HEADER_STATS:
            lines.append(_field(ch.c_type, f"{ch.name}_{stat}"))

    lines += [
        _field("uint8_t", f"reserved1[{spec.HEADER_PAD}]"),
        "} NvHeader;",
        "",
        "/* One settings-journal entry -- exactly one flash doubleword, so a reset",
        "   mid-write leaves it fully written or still blank, never half a setting.",
        "   A blank slot reads all 0xFF and can never pass for an entry (reserved0",
        "   must be 0, and blank reads 0xFFFF there). op_count: lifetime records",
        "   written when this entry was stamped -- non-decreasing along the chain",
        "   (equal is benign: two presses can land inside one record period). */",
        "typedef struct",
        "{",
        _field("uint8_t", "unit_temp", "NV_UNIT_TEMP_*"),
        _field("uint8_t", "unit_press", "NV_UNIT_PRESS_*"),
        _field("uint16_t", "reserved0", "must be 0"),
        _field("uint32_t", "op_count"),
        "} NvJournalEntry;",
        "",
        "typedef struct",
        "{",
        _field("uint32_t", "ts", "seconds since boot"),
    ]

    for ch in spec.CHANNELS:
        comment = f"{ch.unit} x{ch.scale}"
        if ch.note:
            comment = f"{comment} {ch.note}"
        lines.append(_field(ch.c_type, ch.name, comment))

    lines += [
        "} NvRecord;",
        "",
        "/* Compile-time layout locks: if one fires, this header and spec.py have",
        "   drifted -- regenerate, never patch here. GOTCHA: CubeIDE's editor parser",
        "   (not GCC -- every build dialect accepts _Static_assert) flags it as a",
        "   syntax error, so the indexer branch gets a plain C90 negative-array-size",
        "   check instead: same hard compile failure, no phantom squiggles. */",
        "#ifdef __CDT_PARSER__",
        "#define NV_LAYOUT_LOCK(tag, cond)  typedef char nv_layout_lock_##tag[(cond) ? 1 : -1]",
        "#else",
        '#define NV_LAYOUT_LOCK(tag, cond)  _Static_assert(cond, "nv_spec drifted: " #tag)',
        "#endif",
        "",
        _lock("header_size", "sizeof(NvHeader) == NV_HEADER_SIZE"),
        _lock("record_size", "sizeof(NvRecord) == NV_RECORD_SIZE"),
        _lock("header_off_page_seq", f"offsetof(NvHeader, page_seq) == {off_page_seq}U"),
        _lock(f"header_off_{first_stat}", f"offsetof(NvHeader, {first_stat}) == {off_first_stat}U"),
        _lock(f"record_off_{ch0}", f"offsetof(NvRecord, {ch0}) == {off_record_ch0}U"),
        _lock("record_dw_aligned", "(NV_RECORD_SIZE % NV_FLASH_DOUBLEWORD) == 0U"),
        _lock("journal_entry_size", "sizeof(NvJournalEntry) == NV_JOURNAL_ENTRY_SIZE"),
        _lock("journal_atomic", "NV_JOURNAL_ENTRY_SIZE == NV_FLASH_DOUBLEWORD"),
        _lock("journal_off_op_count", "offsetof(NvJournalEntry, op_count) == 4U"),
        _lock("page_fill_exact", "NV_HEADER_SIZE + NV_JOURNAL_SIZE + NV_RECORDS_PER_PAGE * NV_RECORD_SIZE == NV_PAGE_SIZE"),
        "",
        "#endif /* NV_SPEC_H */",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    """Render once; write both firmware copies."""
    text = render()
    for target in TARGETS:
        if not target.parent.is_dir():
            raise SystemExit(f"missing {target.parent} -- run from a full repo checkout")
        with target.open("w", encoding="ascii", newline="\n") as f:
            f.write(text)
        print(f"wrote {target.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
