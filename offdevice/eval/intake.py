"""
Intake check for collaborator anomaly deliveries -- the gate before any scoring.

Sits between a dropped-off batch of anomalous dumps (offdevice/data/anomalies/) and
the eval: nothing is scored until it passes here. Per file it (a) requires the exact
256 KB dump size, (b) identifies the named base among the eight mailed ones and
proves every byte OUTSIDE the 4 KB NV region equals that base -- a diff outside the
region means a rebuilt background, which would let the static hash's territory leak
into the ML eval -- and (c) maps every changed NV byte onto the spec layout (page /
header / journal slot / record + field) so the tampering can be compared to the
filename's label. The eight bases themselves are first re-hashed against their
as-mailed md5s: the file being diffed must be the file that was mailed, or the diff
proves nothing.

Conforming files are written to a DERIVED anomalies_manifest.jsonl (facts computed
here from the bytes, not collaborator paperwork); failures print as a fix-it list
for the collaborator and stay out of the manifest. Re-run on every redelivery --
the manifest is regenerated from scratch each time.

    python -m offdevice.eval.intake
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import numpy as np

from offdevice.data.capture import DEFAULT_CAPTURES_DIR
from offdevice.nv import spec
from offdevice.nv.parse import DUMP_SIZE

DEFAULT_ANOMALIES_DIR = Path(__file__).resolve().parents[1] / "data" / "anomalies"
DEFAULT_BASES_TXT = Path(__file__).resolve().parents[1] / "data" / "collab_bases.txt"
MANIFEST_NAME = "anomalies_manifest.jsonl"

# Delivered filenames carry the ground truth: anom_<tier>__<base>[__<label>].bin
# (base like "steady1_run026" -- single underscore, unlike the capture names'
# double). Two collaborator naming schemes are accepted, differing only in where the
# type is written: the current one names the type in <tier> (anom_blob__ /
# anom_corr__ / anom_stride__ ...), with <label> holding size/variant and sometimes
# absent; the earlier one carried severity in <tier> (obvious/subtle) and the type
# in <label>. The base-md5 and NV-only gate read the bytes, so the scheme only
# decides labelling, never whether a file passes.
NAME_RE = re.compile(r"anom_(?P<tier>\w+?)__(?P<base>\w+?_run\d+)(?:__(?P<label>\w+))?\.bin")

# A <tier> token that names the anomaly type -> its canonical type. When the tier
# is not one of these it is severity, and the type is read from <label> instead.
_TIER_TYPE = {
    "blob": "foreign_blob",
    "corr": "correlation_break",
    "stride": "stride_break",
    "journal": "journal_tamper",
    "erase": "region_erase",
    "mimic": "settings_mimic",
    "defaults": "defaults_downgrade",
    "inrange": "in_range_value",
    "oor": "out_of_range_value",
    "ts": "nonmonotonic_ts",
}

# blob<N> labels are the earlier scheme's foreign-payload size sweep (current
# deliveries use the anom_blob__ tier); the size itself is measured from the diff.
_BLOB_RE = re.compile(r"blob(\d+)")

# Audit detail stored per manifest entry is capped: a whole-region tamper diffs as
# thousands of runs, and the runs are context for humans, not inputs to scoring.
_MAX_RUNS_STORED = 64
_MAX_RUNS_MAPPED = 16

# locate() names record bytes as field + offset by dividing into 4 B lanes; if the
# record layout ever grows a differently-sized field, that division silently lies.
assert spec.RECORD_SIZE == 4 * len(spec.RECORD_FIELDS)


def read_bases(bases_txt: Path) -> dict[str, tuple[str, str]]:
    """collab_bases.txt -> {short_key: (capture_name, as_mailed_md5)}."""
    out: dict[str, tuple[str, str]] = {}
    for line in bases_txt.read_text().splitlines():
        name = line.partition("#")[0].strip()
        if not name:
            continue
        md5 = re.search(r"md5=([0-9a-f]{32})", line)
        key = re.search(r"nv15s-lab\d*-(\w+?)__(run\d+)__", name)
        if md5 is None or key is None:
            raise ValueError(f"{bases_txt.name}: unparseable base line: {line!r}")
        short = f"{key.group(1)}_{key.group(2)}"
        if short in out:
            # Delivered filenames drop the campaign prefix, so two bases collapsing
            # to one short key would silently mis-attribute every diff against it.
            raise ValueError(f"{bases_txt.name}: ambiguous short key '{short}' "
                             f"({out[short][0]} vs {name})")
        out[short] = (name, md5.group(1))
    return out


def locate(nv_off: int) -> str:
    """Name the spec-layout home of one NV-relative byte offset (audit context)."""
    page, o = divmod(nv_off, spec.PAGE_SIZE)
    if o < spec.JOURNAL_OFFSET:
        return f"p{page} header+0x{o:02X}"
    if o < spec.RECORDS_OFFSET:
        slot, b = divmod(o - spec.JOURNAL_OFFSET, spec.JOURNAL_ENTRY_SIZE)
        return f"p{page} journal J{slot}+{b}"
    rec, b = divmod(o - spec.RECORDS_OFFSET, spec.RECORD_SIZE)
    return f"p{page} rec{rec}.{spec.RECORD_FIELDS[b // 4]}+{b % 4}"


def diff_runs(a: bytes, b: bytes) -> list[tuple[int, int]]:
    """Contiguous (start, length) runs where the two equal-length buffers differ."""
    diff = np.flatnonzero(np.frombuffer(a, np.uint8) != np.frombuffer(b, np.uint8))
    runs: list[tuple[int, int]] = []
    for off in diff.tolist():
        if runs and off == runs[-1][0] + runs[-1][1]:
            runs[-1] = (runs[-1][0], runs[-1][1] + 1)
        else:
            runs.append((off, 1))
    return runs


def check_file(path: Path, bases: dict[str, tuple[str, str]],
               captures_dir: Path) -> tuple[dict[str, object] | None, str | None]:
    """One delivered file -> (manifest entry, None) or (None, fix-it reason)."""
    m = NAME_RE.fullmatch(path.name)
    if m is None:
        return None, "filename does not follow anom_<tier>__<base>[__<label>].bin"
    data = path.read_bytes()
    if len(data) != DUMP_SIZE:
        return None, f"size {len(data)} != {DUMP_SIZE} (whole 256 KB dump required)"
    base_key = m["base"]
    if base_key not in bases:
        return None, f"base '{base_key}' is not one of the eight mailed bases"
    base_name, base_md5 = bases[base_key]
    base = (captures_dir / base_name).read_bytes()

    if data[:spec.DUMP_OFFSET] != base[:spec.DUMP_OFFSET]:
        n_out = len(diff_runs(data[:spec.DUMP_OFFSET], base[:spec.DUMP_OFFSET]))
        return None, (f"{n_out} changed run(s) OUTSIDE the NV region -- rebuilt "
                      f"background; only bytes at 0x{spec.DUMP_OFFSET:X}+ may differ")
    runs = diff_runs(data[spec.DUMP_OFFSET:], base[spec.DUMP_OFFSET:])
    if not runs:
        return None, "byte-identical to its base -- no tampering present"

    # Type comes from <tier> when it names one (current scheme), else from <label>,
    # which the earlier scheme set to the type (blob<size> being a foreign blob).
    label = m["label"]
    if m["tier"] in _TIER_TYPE:
        typ = _TIER_TYPE[m["tier"]]
    elif label and _BLOB_RE.fullmatch(label):
        typ = "foreign_blob"
    elif label:
        typ = label
    else:
        return None, "cannot determine type: unrecognized tier and no label"
    entry: dict[str, object] = {
        "file": path.name,
        "md5": hashlib.md5(data).hexdigest(),
        "tier": m["tier"],
        "label": label or "",
        "type": typ,
        "base": base_name,
        "base_md5": base_md5,
        "nv_changed_bytes": int(sum(n for _, n in runs)),
        "n_runs": len(runs),
        "runs": [[s, n] for s, n in runs[:_MAX_RUNS_STORED]],
        "run_map": [f"0x{s:03X}+{n}: {locate(s)} .. {locate(s + n - 1)}"
                    for s, n in runs[:_MAX_RUNS_MAPPED]],
    }
    if len(runs) > _MAX_RUNS_STORED:
        entry["runs_truncated"] = True
    return entry, None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verify a collaborator anomaly delivery and write the derived manifest.")
    ap.add_argument("--anomalies-dir", type=Path, default=DEFAULT_ANOMALIES_DIR)
    ap.add_argument("--bases", type=Path, default=DEFAULT_BASES_TXT)
    ap.add_argument("--captures-dir", type=Path, default=DEFAULT_CAPTURES_DIR)
    args = ap.parse_args()

    bases = read_bases(args.bases)
    for key, (name, md5_mailed) in bases.items():
        p = args.captures_dir / name
        if not p.exists():
            print(f"[intake] base capture missing: {name}")
            return 2
        if hashlib.md5(p.read_bytes()).hexdigest() != md5_mailed:
            print(f"[intake] base {name} on disk does NOT match its as-mailed md5 -- "
                  f"the reference is compromised; resolve before any intake")
            return 2
    print(f"[intake] all {len(bases)} bases match their as-mailed md5s")

    files = sorted(args.anomalies_dir.glob("*.bin"))
    if not files:
        print(f"[intake] no .bin files in {args.anomalies_dir}")
        return 1

    entries: list[dict[str, object]] = []
    fixit: list[tuple[str, str]] = []
    seen_md5: dict[str, str] = {}
    for path in files:
        entry, reason = check_file(path, bases, args.captures_dir)
        if entry is None:
            fixit.append((path.name, str(reason)))
            continue
        dup = seen_md5.get(str(entry["md5"]))
        if dup is not None:
            fixit.append((path.name, f"byte-identical to {dup} (duplicate delivery)"))
            continue
        seen_md5[str(entry["md5"])] = path.name
        entries.append(entry)
        print(f"[intake] PASS {path.name}: {entry['nv_changed_bytes']} NV byte(s) "
              f"in {entry['n_runs']} run(s), first at {entry['run_map'][0]}")

    manifest = args.anomalies_dir / MANIFEST_NAME
    with manifest.open("w", newline="\n") as f:
        f.write(json.dumps({"derived_by": "offdevice.eval.intake",
                            "nv_spec_version": spec.SPEC_VERSION,
                            "n_files": len(entries)}) + "\n")
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    print(f"[intake] wrote {manifest} ({len(entries)} of {len(files)} files)")

    if fixit:
        print(f"\n[intake] FIX-IT LIST ({len(fixit)} file(s) NOT in the manifest):")
        for name, reason in fixit:
            print(f"  {name}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
