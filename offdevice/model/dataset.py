"""
Benign-dataset assembly: manifest records -> model-ready feature vectors + fill states.

Sits between the capture toolchain (offdevice/data) and the one-class model
(offdevice/model/fit.py): filter the manifest to the requested benign campaign tags,
slice each capture's 4 KB NV region, run the frozen feature pipeline, and flatten each
(40, 3) matrix to the model's flat input vector. Records are filtered BEFORE any bytes
load, and every file that is read is re-verified against its manifest length and md5.
Each capture also gets its ring FILL STATE (empty -> near-empty -> pre-wrap ->
just-wrapped -> steady), derived from the parsed page headers -- the collector can't
record it (it never parses the ring), and the holdout split stratifies on it.

Benign-strict: a structural layout violation (foreign page, dirty tail, broken ring
invariants) in a capture labeled benign aborts assembly -- that byte pattern is the
very anomaly class the IDS hunts, and it must never train the model silently. The
designed exit for such a capture is the committed quarantine list (one filename +
reason per line): quarantined names are skipped by every model path while the
manifest itself stays append-only. Out-of-range VALUES only mark the sample
(range_ok=False): they point at a generator bug or a torn write, not tampering, and
the caller decides (fit.py refuses them by default).

Eyeball a tag's captures (fill states, NaN/range sanity) before fitting:
    python -m offdevice.model.dataset nv45s-smoke
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt

from offdevice.data.capture import DEFAULT_CAPTURES_DIR, MANIFEST_NAME
from offdevice.data.format import DumpRecord
from offdevice.data.loader import load_dump_bytes, resolve_dump_path
from offdevice.data.manifest import read_manifest
from offdevice.features import params
from offdevice.features.extract import extract_features
from offdevice.nv import spec
from offdevice.nv.parse import (
    PageView,
    RegionView,
    journal_chain,
    parse_region,
    records_chronological,
    slice_nv,
)

DEFAULT_MANIFEST = DEFAULT_CAPTURES_DIR / MANIFEST_NAME

# The committed retraction list, sibling of holdout.txt. The manifest is
# append-only, so a capture that fails the benign-structure gate is never edited
# out of it -- its bare filename goes here (with a '# reason'), and every model
# path (assembly, split, fit, holdout scoring) skips it. Auditable by design.
DEFAULT_QUARANTINE = DEFAULT_CAPTURES_DIR.parent / "quarantine.txt"

# The model's input dimension: the (40, 3) feature matrix flattened feature-major.
N_DIMS = params.N_BINS * params.N_FEATURES

# Ring fill states, the holdout-split strata: the benign byte regimes different enough
# to each need representation in both the training and the held-out exam set.
FILL_STATES = ("empty", "near-empty", "pre-wrap", "just-wrapped", "steady")

# Settings strata, the split's second dimension: a journal change entry (anything
# beyond J0) is extra ink where quiet pages read 0xFF -- a benign byte regime the
# exam set must cover too.
SETTINGS_STATES = ("settings-quiet", "settings-changed")

# A u32 field that reads all-0xFF was never programmed: the tell of a record torn
# by a reset landing inside its two-doubleword program window.
_ERASED_U32 = int.from_bytes(bytes([spec.ERASED_BYTE]) * 4, "little")


@dataclass(frozen=True)
class Sample:
    """One capture, model-ready: provenance + flat feature vector + ring context."""

    record: DumpRecord
    x: npt.NDArray[np.float32]   # (N_DIMS,) flat feature vector
    fill_state: str              # one of FILL_STATES
    settings_state: str          # one of SETTINGS_STATES
    n_records: int               # ring records present at capture time
    n_torn: int                  # records whose trailing doubleword reads erased
    range_ok: bool               # every stored value inside its channel's legal range


def read_name_list(path: str | Path) -> frozenset[str]:
    """Bare filenames from a one-name-per-line .txt ('#' comments ignored)."""
    names: set[str] = set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(line)
    return frozenset(names)


def read_quarantine(path: str | Path = DEFAULT_QUARANTINE) -> frozenset[str]:
    """The quarantined bare filenames; a missing file means no retractions yet."""
    p = Path(path)
    return read_name_list(p) if p.exists() else frozenset()


def flatten_features(feats: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    """(40, 3) feature matrix -> the model's flat (120,) vector, feature-major.

    Order: all 40 mfcc bins, then all 40 mel, then all 40 chroma_stft -- i.e. the
    columns of params.FEATURE_ORDER concatenated. The on-chip scorer must fill its
    vector in this exact order or it scores against a scrambled mean.
    """
    if feats.shape != params.FEATURE_SHAPE:
        raise ValueError(f"expected {params.FEATURE_SHAPE} features, got {feats.shape}")
    return np.ascontiguousarray(feats.T).reshape(-1)


def fill_state(view: RegionView) -> str:
    """Classify the ring's fill state from its parsed headers.

    Stratification labels only -- the model never sees them. page_seq counts
    page-opens, so a seq above NUM_PAGES means some page was erased and reopened
    (the ring wrapped); the first wrap cycle still carries a virgin-fill header
    mix, hence the separate just-wrapped stratum before steady state.
    """
    if view.current is None:
        return "empty"
    header = view.pages[view.current].header
    assert header is not None   # current is by construction a valid-header page
    max_seq = header["page_seq"]
    if max_seq <= spec.NUM_PAGES:
        n = len(records_chronological(view))
        return "near-empty" if n <= spec.RECORDS_PER_PAGE // 2 else "pre-wrap"
    if max_seq <= 2 * spec.NUM_PAGES:
        return "just-wrapped"
    return "steady"


def settings_state(view: RegionView) -> str:
    """"settings-changed" when any page's journal chain holds an entry beyond J0.

    Stratification label only, like fill_state -- the model never sees it. J0
    alone is the page-open stamp every opened page carries; anything after it is
    a runtime settings change, and those pages are a benign byte regime the
    held-out exam must cover as well as training.
    """
    for page in view.pages:
        if page.header is not None and len(journal_chain(page)) > 1:
            return "settings-changed"
    return "settings-quiet"


def is_torn(rec: dict[str, int]) -> bool:
    """True when a record's trailing doubleword (hum + press) reads erased.

    A reset inside the record-program window leaves the second of the two
    doublewords unprogrammed; the firmware's head scan skips such a slot. The
    resulting 0xFFFFFFFF values fail the range check, but they are operational
    wear-and-tear (probable torn write), not a generator bug.
    """
    return rec["hum"] == _ERASED_U32 and rec["press"] == _ERASED_U32


def check_spec_version(nv: bytes, name: str) -> None:
    """Refuse rehearsal-era captures by name: a page whose header says spec v1.

    Belt-and-suspenders: a v1 header no longer parses (version mismatch), so such
    a page would abort as FOREIGN anyway -- but the banked v1 captures are the one
    known population of such files, and they deserve a diagnosis, not a scary
    FOREIGN message. Fires only on a PLAUSIBLE v1 header (version 1, zero
    reserved0, sane page_seq -- v1's own validity rule): foreign ink that merely
    starts with 0x0001 (e.g. the old proof-demo counter doubleword) stays the
    FOREIGN gate's diagnosis.
    """
    for i in range(spec.NUM_PAGES):
        page = nv[i * spec.PAGE_SIZE : (i + 1) * spec.PAGE_SIZE]
        if page[: spec.HEADER_SIZE] == spec.BLANK_HEADER:
            continue
        version = int.from_bytes(page[0:2], "little")
        reserved0 = int.from_bytes(page[2:4], "little")
        page_seq = int.from_bytes(page[4:8], "little")
        if version == 1 and reserved0 == 0 and 1 <= page_seq < 0xFFFFFFFF:
            raise ValueError(f"{name}: page{i} header says spec v1 -- a rehearsal-era "
                             f"capture; v1 data never trains or exams the v2 model")


def _check_journal(name: str, i: int, page: PageView) -> None:
    """The settings-journal chain invariants for one opened page.

    Blank-slot purity needs no separate check: any slot that is not all-0xFF
    parses as a written entry and must then clear every rule below -- a
    partially-programmed "blank" cannot slip through as ignored space.
    """
    header = page.header
    assert header is not None
    chain = journal_chain(page)
    if not chain:
        raise ValueError(f"{name}: page{i} has no J0 -- page-open always stamps the "
                         f"live settings into slot 0")
    if not page.journal_tail_clean:
        raise ValueError(f"{name}: page{i} journal has a written slot after a blank "
                         f"(the chain is append-only and contiguous)")
    for j, entry in enumerate(chain):
        if entry["reserved0"] != 0:
            raise ValueError(f"{name}: page{i} J{j} reserved0=0x{entry['reserved0']:04X} "
                             f"(must be 0 -- monitored blank space)")
        if (entry["unit_temp"] not in (spec.UNIT_TEMP_C, spec.UNIT_TEMP_F)
                or entry["unit_press"] not in (spec.UNIT_PRESS_HPA, spec.UNIT_PRESS_INHG)):
            raise ValueError(f"{name}: page{i} J{j} units ({entry['unit_temp']}, "
                             f"{entry['unit_press']}) outside {{0, 1}}")
        delta = entry["op_count"] - header["op_count"]
        if not 0 <= delta <= len(page.records):
            raise ValueError(f"{name}: page{i} J{j} op_count {entry['op_count']} outside "
                             f"the page's record window [{header['op_count']}, "
                             f"{header['op_count'] + len(page.records)}]")
    if chain[0]["op_count"] != header["op_count"]:
        raise ValueError(f"{name}: page{i} J0 op_count {chain[0]['op_count']} != header "
                         f"op_count {header['op_count']} (J0 is stamped at page-open)")
    for j in range(1, len(chain)):
        if chain[j]["op_count"] < chain[j - 1]["op_count"]:
            raise ValueError(f"{name}: page{i} journal op_counts decrease at J{j} "
                             f"(non-decreasing along the chain; equal is benign)")


def check_benign_structure(view: RegionView, name: str) -> None:
    """Abort on a structural layout violation in a benign-labeled capture.

    A foreign page (bytes without a valid header -- e.g. the old proof-demo counter
    doubleword) or a written slot after the head is exactly the structural corruption
    the detector exists to flag; training on it would teach the model that tampering
    is normal. Quarantine the capture (offdevice/data/quarantine.txt) / investigate
    instead. Beyond the per-page checks, the ring's cross-page invariants hold in
    every benign capture: a page opens only when its predecessor is FULL, stamping
    seq+1 and op_count += RECORDS_PER_PAGE -- any other pair of valid headers is a
    state the logger cannot write. Every opened page must also carry a benign
    settings-journal chain (_check_journal). These gates live HERE, not in
    nv/parse.py: the eval path must push corrupted regions through a neutral
    parser.
    """
    for i, page in enumerate(view.pages):
        if page.header is None and not page.blank:
            raise ValueError(f"{name}: page{i} has bytes but no valid header (FOREIGN)")
        if not page.tail_clean:
            raise ValueError(f"{name}: page{i} has a written slot after the head (dirty tail)")
        if not page.pad_clean:
            raise ValueError(f"{name}: page{i} header pad bytes are not all "
                             f"0x{spec.HEADER_PAD_FILL:02X} (rewritten or foreign header)")
        if page.header is not None:
            _check_journal(name, i, page)

    h0, h1 = view.pages[0].header, view.pages[1].header
    if h0 is None or h1 is None:
        return
    if h0["page_seq"] == h1["page_seq"]:
        # The firmware treats equal seqs as corrupt and wipes them at boot, so no
        # benign capture (taken pre-Init or post) can legitimately show them.
        raise ValueError(f"{name}: both pages claim page_seq={h0['page_seq']} (corrupt ring)")
    assert view.current is not None
    older = view.pages[1 - view.current]
    hn, ho = view.pages[view.current].header, older.header
    assert hn is not None and ho is not None
    if hn["page_seq"] != ho["page_seq"] + 1:
        raise ValueError(f"{name}: page seqs {ho['page_seq']} -> {hn['page_seq']} not "
                         f"consecutive (page-opens increment by exactly 1)")
    if len(older.records) != spec.RECORDS_PER_PAGE:
        raise ValueError(f"{name}: older page holds {len(older.records)}/"
                         f"{spec.RECORDS_PER_PAGE} records -- a page opens only when "
                         f"its predecessor is full")
    if hn["op_count"] != ho["op_count"] + spec.RECORDS_PER_PAGE:
        raise ValueError(f"{name}: op_count chain broken ({ho['op_count']} + "
                         f"{spec.RECORDS_PER_PAGE} != {hn['op_count']})")


def values_in_range(view: RegionView) -> bool:
    """True when every stored record value sits inside its channel's legal range."""
    for rec in records_chronological(view):
        for ch in spec.CHANNELS:
            if not (ch.lo <= rec[ch.name] <= ch.hi):
                return False
    return True


def load_samples(
    manifest_path: str | Path,
    variants: tuple[str, ...],
    *,
    exclude: frozenset[str] | set[str] = frozenset(),
    quarantine: frozenset[str] | set[str] | None = None,
    root: str | Path | None = None,
) -> list[Sample]:
    """Assemble model-ready samples for the given campaign tags.

    Keeps records with label "benign" and conditions["variant"] in `variants`;
    `exclude` (bare filenames -- the holdout list) and the quarantine list
    (None = read the committed default) are dropped after that filter. Filtering
    happens BEFORE any bytes load, so a missing or corrupt file outside the
    requested scope can't break assembly; each file that IS read is re-verified
    against its manifest length and md5. Order follows the manifest (append-only,
    so it is stable and chronological).
    """
    if quarantine is None:
        quarantine = read_quarantine()
    wanted = set(variants)
    manifest_path = Path(manifest_path)
    base = Path(root) if root is not None else manifest_path.parent
    samples: list[Sample] = []
    for record in read_manifest(manifest_path):
        if record.label != "benign" or record.conditions.get("variant") not in wanted:
            continue
        name = Path(record.file).name
        if name in exclude or name in quarantine:
            continue
        if record.sr != params.SR:
            raise ValueError(f"{record.file}: manifest sr={record.sr} != params.SR="
                             f"{params.SR} -- the capture targets a different feature "
                             f"contract")
        raw = load_dump_bytes(resolve_dump_path(record, base),
                              expect_bytes=record.n_bytes, expect_md5=record.md5)
        nv = slice_nv(raw)
        check_spec_version(nv, record.file)
        view = parse_region(nv)
        check_benign_structure(view, record.file)
        recs = records_chronological(view)
        samples.append(Sample(
            record=record,
            x=flatten_features(extract_features(nv)),
            fill_state=fill_state(view),
            settings_state=settings_state(view),
            n_records=len(recs),
            n_torn=sum(1 for r in recs if is_torn(r)),
            range_ok=values_in_range(view),
        ))
    return samples


def design_matrix(samples: list[Sample]) -> npt.NDArray[np.float64]:
    """Stack sample vectors into the (n_samples, N_DIMS) float64 fit input.

    float64 on the host: the covariance fit and its inversion want the headroom;
    the on-chip port quantizes from the exported artifact, not from here.
    """
    if not samples:
        raise ValueError("no samples to stack")
    return np.stack([s.x for s in samples]).astype(np.float64)


# ---- CLI: per-capture sanity table ------------------------------------------------


def main(argv: list[str]) -> int:
    """Print one line per matching capture: fill state, record count, NaN/range checks."""
    if not argv:
        print("usage: python -m offdevice.model.dataset <variant-tag> [<variant-tag> ...]")
        return 2
    quarantine = read_quarantine()
    if quarantine:
        print(f"{len(quarantine)} quarantined name(s) excluded per {DEFAULT_QUARANTINE}")
    samples = load_samples(DEFAULT_MANIFEST, tuple(argv), quarantine=quarantine)
    if not samples:
        print(f"no benign captures in {DEFAULT_MANIFEST} match variants {argv}")
        return 1

    counts: dict[str, int] = {}
    set_counts: dict[str, int] = {}
    for s in samples:
        counts[s.fill_state] = counts.get(s.fill_state, 0) + 1
        set_counts[s.settings_state] = set_counts.get(s.settings_state, 0) + 1
        nan = "NaN!" if np.isnan(s.x).any() else "ok"
        if s.range_ok:
            rng = "ok"
        elif s.n_torn:
            rng = f"TORN-WRITE?x{s.n_torn}"
        else:
            rng = "OUT-OF-RANGE"
        print(f"{s.record.file:60s} {s.fill_state:12s} {s.settings_state:16s} "
              f"recs={s.n_records:3d} nan={nan:4s} range={rng}")
    state_summary = "  ".join(f"{st}={counts[st]}" for st in FILL_STATES if st in counts)
    settings_summary = "  ".join(f"{st}={set_counts[st]}"
                                 for st in SETTINGS_STATES if st in set_counts)
    print(f"\n{len(samples)} captures: {state_summary}  |  {settings_summary}")
    by_md5: dict[str, list[str]] = {}
    for s in samples:
        by_md5.setdefault(s.record.md5, []).append(Path(s.record.file).name)
    dupes = {m: names for m, names in by_md5.items() if len(names) > 1}
    if dupes:
        groups = "; ".join(f"{m[:8]}...: {', '.join(names)}" for m, names in dupes.items())
        print(f"WARNING: byte-identical duplicate captures share an md5 -- they "
              f"double-weight training and can leak the holdout exam (quarantine "
              f"all but one): {groups}")
    bad = [s for s in samples if not s.range_ok]
    if bad:
        torn = sum(1 for s in bad if s.n_torn)
        parts = []
        if torn:
            parts.append(f"{torn} probable torn write(s) (reset during a record "
                         f"program -- hum+press read erased)")
        if len(bad) - torn:
            parts.append(f"{len(bad) - torn} true range violation(s) (generator bug?)")
        print(f"WARNING: {len(bad)} capture(s) hold out-of-range values: "
              + "; ".join(parts)
              + " -- fit.py refuses them unless --allow-out-of-range")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
