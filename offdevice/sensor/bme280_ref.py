"""
BME280 compensation reference: re-runs the firmware's integer math on the
self-test console dump and demands integer-for-integer agreement.

This is the sensor-path parity gate (same discipline as engine/'s scorer
parity): firmware/.../bme280.c transcribes the Bosch datasheet's integer
compensation code (BST-BME280-DS002 ch. 4.2.3), and this module transcribes
the SAME code in Python. The firmware prints its raw calibration bytes, its
assembled trim words, and raw/compensated/spec-scaled vectors; this module
re-derives every stage independently -- trim assembly from the raw bytes,
compensation from the raw ADC codes, spec scaling from the compensated
values -- and compares. Any mismatch fails loudly; no campaign data is
collected until it passes.

Faithfulness notes: Python's >> on negatives is arithmetic (matches ARM GCC);
the one division in the pressure path truncates toward zero in C, mirrored by
_truncdiv(). C int32/int64 overflow is NOT emulated -- if the firmware ever
wrapped, the outputs would simply disagree and the gate would fail, which is
the correct outcome.

Check a captured console dump:
    python -m offdevice.sensor.bme280_ref path\\to\\selftest_console.txt
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ---- trim words (datasheet Table 16) ----------------------------------------


@dataclass(frozen=True)
class Trim:
    """The 18 factory calibration words, exactly as the chip stores them."""

    T1: int  # u16
    T2: int  # s16
    T3: int  # s16
    P1: int  # u16
    P2: int  # s16
    P3: int  # s16
    P4: int  # s16
    P5: int  # s16
    P6: int  # s16
    P7: int  # s16
    P8: int  # s16
    P9: int  # s16
    H1: int  # u8
    H2: int  # s16
    H3: int  # u8
    H4: int  # s12 (packed across 0xE4/0xE5)
    H5: int  # s12 (packed across 0xE5/0xE6)
    H6: int  # s8


def _u16(lo: int, hi: int) -> int:
    return (hi << 8) | lo


def _s16(lo: int, hi: int) -> int:
    v = (hi << 8) | lo
    return v - 0x10000 if v >= 0x8000 else v


def _s8(b: int) -> int:
    return b - 0x100 if b >= 0x80 else b


def unpack_trim(calib_a: bytes, calib_b: bytes) -> Trim:
    """Assemble trim words from the raw calib blobs (0x88..0xA1 and 0xE1..0xE7).

    Mirrors bme280.c's bme_unpack_trim bit-for-bit, including the sign-extended
    H4/H5 nibble packing around the shared 0xE5 byte (Bosch reference driver).
    """
    if len(calib_a) != 26 or len(calib_b) != 7:
        raise ValueError(f"calib blob sizes {len(calib_a)}/{len(calib_b)}, want 26/7")
    a, b = calib_a, calib_b
    return Trim(
        T1=_u16(a[0], a[1]), T2=_s16(a[2], a[3]), T3=_s16(a[4], a[5]),
        P1=_u16(a[6], a[7]), P2=_s16(a[8], a[9]), P3=_s16(a[10], a[11]),
        P4=_s16(a[12], a[13]), P5=_s16(a[14], a[15]), P6=_s16(a[16], a[17]),
        P7=_s16(a[18], a[19]), P8=_s16(a[20], a[21]), P9=_s16(a[22], a[23]),
        H1=a[25],
        H2=_s16(b[0], b[1]),
        H3=b[2],
        H4=(_s8(b[3]) * 16) | (b[4] & 0x0F),
        H5=(_s8(b[5]) * 16) | (b[4] >> 4),
        H6=_s8(b[6]),
    )


# ---- Bosch integer compensation (datasheet 4.2.3, transcribed verbatim) -----


def _truncdiv(a: int, b: int) -> int:
    """C-style integer division: truncate toward zero (Python // floors)."""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def compensate_t_int32(adc_t: int, trim: Trim) -> tuple[int, int]:
    """Temperature in degC x100 plus the t_fine carrier the P/H paths consume."""
    var1 = ((((adc_t >> 3) - (trim.T1 << 1))) * trim.T2) >> 11
    var2 = (((((adc_t >> 4) - trim.T1) * ((adc_t >> 4) - trim.T1)) >> 12) * trim.T3) >> 14
    t_fine = var1 + var2
    t = (t_fine * 5 + 128) >> 8
    return t, t_fine


def compensate_p_int64(adc_p: int, t_fine: int, trim: Trim) -> int:
    """Pressure in Pa as unsigned Q24.8 (24 integer + 8 fraction bits)."""
    var1 = t_fine - 128000
    var2 = var1 * var1 * trim.P6
    var2 = var2 + ((var1 * trim.P5) << 17)
    var2 = var2 + (trim.P4 << 35)
    var1 = ((var1 * var1 * trim.P3) >> 8) + ((var1 * trim.P2) << 12)
    var1 = ((1 << 47) + var1) * trim.P1 >> 33
    if var1 == 0:
        return 0  # avoid exception caused by division by zero
    p = 1048576 - adc_p
    p = _truncdiv(((p << 31) - var2) * 3125, var1)
    var1 = (trim.P9 * (p >> 13) * (p >> 13)) >> 25
    var2 = (trim.P8 * p) >> 19
    p = ((p + var1 + var2) >> 8) + (trim.P7 << 4)
    return p & 0xFFFFFFFF  # the C code returns (BME280_U32_t)p


def compensate_h_int32(adc_h: int, t_fine: int, trim: Trim) -> int:
    """Humidity in RH as unsigned Q22.10 (22 integer + 10 fraction bits)."""
    v = t_fine - 76800
    v = ((((adc_h << 14) - (trim.H4 << 20) - (trim.H5 * v)) + 16384) >> 15) * (
        ((((((v * trim.H6) >> 10) * (((v * trim.H3) >> 11) + 32768)) >> 10) + 2097152)
         * trim.H2 + 8192) >> 14)
    v = v - ((((v >> 15) * (v >> 15)) >> 7) * trim.H1 >> 4)
    v = 0 if v < 0 else v
    v = 419430400 if v > 419430400 else v
    return v >> 12


def to_spec_scales(comp_t: int, comp_p: int, comp_h: int) -> tuple[int, int, int]:
    """(temp degC x100, hum RH x100, press Pa) -- the nv_spec.h record fields.

    Round-to-nearest, mirroring bme280.c: temp passes through; press drops the
    Q24.8 fraction; hum converts Q22.10 to x100 (x100/1024 == x25/256).
    """
    return comp_t, (comp_h * 25 + 128) >> 8, (comp_p + 128) >> 8


# ---- self-test console parsing ----------------------------------------------

_RE_CALIB_A = re.compile(r"\[BME\] calibA ([0-9A-Fa-f]{52})")
_RE_CALIB_B = re.compile(r"\[BME\] calibB ([0-9A-Fa-f]{14})")
_RE_TRIM_T = re.compile(r"\[BME\] trimT T1=(\d+) T2=(-?\d+) T3=(-?\d+)")
_RE_TRIM_P = re.compile(
    r"\[BME\] trimP P1=(\d+) P2=(-?\d+) P3=(-?\d+) P4=(-?\d+) P5=(-?\d+)"
    r" P6=(-?\d+) P7=(-?\d+) P8=(-?\d+) P9=(-?\d+)")
_RE_TRIM_H = re.compile(r"\[BME\] trimH H1=(\d+) H2=(-?\d+) H3=(\d+) H4=(-?\d+) H5=(-?\d+) H6=(-?\d+)")
_RE_RAW = re.compile(r"\[BME\] vec(\d+) raw ut=(\d+) up=(\d+) uh=(\d+)")
_RE_CMP = re.compile(r"\[BME\] vec(\d+) cmp tfine=(-?\d+) T=(-?\d+) P=(\d+) H=(\d+)")
_RE_REC = re.compile(r"\[BME\] vec(\d+) rec temp=(-?\d+) hum=(\d+) press=(\d+)")


@dataclass(frozen=True)
class SelfTestDump:
    """Everything the firmware self-test printed, parsed."""

    calib_a: bytes
    calib_b: bytes
    fw_trim: Trim
    vectors: tuple[dict[str, int], ...]   # per vec: ut up uh tfine T P H temp hum press


def parse_selftest(text: str) -> SelfTestDump:
    """Parse a self-test console capture; raise ValueError on anything missing."""
    m_a, m_b = _RE_CALIB_A.search(text), _RE_CALIB_B.search(text)
    m_t, m_p, m_h = _RE_TRIM_T.search(text), _RE_TRIM_P.search(text), _RE_TRIM_H.search(text)
    if not (m_a and m_b and m_t and m_p and m_h):
        raise ValueError("console text is missing calibA/calibB/trim lines -- "
                         "is this a BME280_SELFTEST=1 boot capture?")
    t = [int(x) for x in m_t.groups()]
    p = [int(x) for x in m_p.groups()]
    h = [int(x) for x in m_h.groups()]
    fw_trim = Trim(T1=t[0], T2=t[1], T3=t[2],
                   P1=p[0], P2=p[1], P3=p[2], P4=p[3], P5=p[4],
                   P6=p[5], P7=p[6], P8=p[7], P9=p[8],
                   H1=h[0], H2=h[1], H3=h[2], H4=h[3], H5=h[4], H6=h[5])

    raws = {int(m.group(1)): m for m in _RE_RAW.finditer(text)}
    cmps = {int(m.group(1)): m for m in _RE_CMP.finditer(text)}
    recs = {int(m.group(1)): m for m in _RE_REC.finditer(text)}
    if not raws or raws.keys() != cmps.keys() or raws.keys() != recs.keys():
        raise ValueError(f"vector lines incomplete: raw={sorted(raws)} cmp={sorted(cmps)} rec={sorted(recs)}")

    vectors = []
    for n in sorted(raws):
        r, c, k = raws[n], cmps[n], recs[n]
        vectors.append({
            "ut": int(r.group(2)), "up": int(r.group(3)), "uh": int(r.group(4)),
            "tfine": int(c.group(2)), "T": int(c.group(3)), "P": int(c.group(4)), "H": int(c.group(5)),
            "temp": int(k.group(2)), "hum": int(k.group(3)), "press": int(k.group(4)),
        })
    return SelfTestDump(calib_a=bytes.fromhex(m_a.group(1)), calib_b=bytes.fromhex(m_b.group(1)),
                        fw_trim=fw_trim, vectors=tuple(vectors))


# ---- the gate ----------------------------------------------------------------


def check_parity(dump: SelfTestDump) -> list[str]:
    """Re-derive every stage and return a list of mismatch descriptions (empty = PASS)."""
    fails: list[str] = []

    ref_trim = unpack_trim(dump.calib_a, dump.calib_b)
    for name in Trim.__dataclass_fields__:
        fw, ref = getattr(dump.fw_trim, name), getattr(ref_trim, name)
        if fw != ref:
            fails.append(f"trim {name}: firmware assembled {fw}, reference assembled {ref}")

    for i, v in enumerate(dump.vectors, start=1):
        t, t_fine = compensate_t_int32(v["ut"], ref_trim)
        p = compensate_p_int64(v["up"], t_fine, ref_trim)
        h = compensate_h_int32(v["uh"], t_fine, ref_trim)
        temp, hum, press = to_spec_scales(t, p, h)
        for name, fw, ref in (("tfine", v["tfine"], t_fine), ("T", v["T"], t),
                              ("P", v["P"], p), ("H", v["H"], h),
                              ("temp", v["temp"], temp), ("hum", v["hum"], hum),
                              ("press", v["press"], press)):
            if fw != ref:
                fails.append(f"vec{i} {name}: firmware {fw}, reference {ref}")
    return fails


def main(argv: list[str]) -> int:
    """CLI: parse a console capture, check parity, report PASS/FAIL."""
    if len(argv) != 1:
        print("usage: python -m offdevice.sensor.bme280_ref <selftest_console.txt>")
        return 2
    try:
        dump = parse_selftest(Path(argv[0]).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        print(f"cannot check parity: {e}")
        return 2
    fails = check_parity(dump)

    print(f"trim words cross-checked from raw calib bytes: {'ok' if not any(f.startswith('trim') for f in fails) else 'MISMATCH'}")
    for i, v in enumerate(dump.vectors, start=1):
        sign = "-" if v["temp"] < 0 else ""
        at = abs(v["temp"])
        print(f"vec{i}: temp={sign}{at // 100}.{at % 100:02d} C  "
              f"hum={v['hum'] // 100}.{v['hum'] % 100:02d} %RH  "
              f"press={v['press'] // 100}.{v['press'] % 100:02d} hPa")
    if fails:
        print(f"\nBME280 PARITY FAIL ({len(fails)} mismatches):")
        for f in fails:
            print(f"  {f}")
        return 1
    print(f"\nBME280 PARITY PASS ({len(dump.vectors)} vectors, trim assembly + compensation + spec scaling)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
