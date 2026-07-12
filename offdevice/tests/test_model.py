"""
Unit tests for the one-class model package: dataset assembly (flatten order, fill
states, benign-strictness), the stratified holdout split, and the Mahalanobis
fit/score/threshold/artifact path.

Run from the repo root (so `import offdevice...` resolves):
    pytest offdevice\\tests\\test_model.py -v
"""

from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

from offdevice.data.format import DumpRecord
from offdevice.data.manifest import write_manifest
from offdevice.features.extract import extract_features
from offdevice.model import dataset as dataset_mod
from offdevice.model import score
from offdevice.model.dataset import (
    N_DIMS,
    Sample,
    check_benign_structure,
    fill_state,
    flatten_features,
    load_samples,
    settings_state,
)
from offdevice.model.fit import (
    MahalanobisModel,
    distances,
    fit_mean_precision,
    load_model,
    loo_distances,
    save_model,
    threshold_from_loo,
)
from offdevice.model.split import (
    choose_holdout,
    read_holdout,
    read_holdout_variants,
    write_holdout,
)
from offdevice.nv import spec
from offdevice.nv.parse import DUMP_SIZE, parse_region
from offdevice.tests.fixtures import synthetic_nv_region


# ---- synthetic builders (mirror test_nv_parse's, sized for fill-state control) ----

def make_header(**over: int) -> bytes:
    fields = dict.fromkeys(spec.HEADER_FIELDS, 0)
    fields.update(version=spec.SPEC_VERSION, page_seq=1, boot_count=1)
    fields.update(over)
    return struct.pack(spec.HEADER_FMT, *(fields[f] for f in spec.HEADER_FIELDS))


def make_record(ts: int, temp: int = 2200, hum: int = 4500, press: int = 101300) -> bytes:
    return struct.pack(spec.RECORD_FMT, ts, temp, hum, press)


def make_journal_entry(**over: int) -> bytes:
    fields = dict.fromkeys(spec.JOURNAL_FIELDS, 0)
    fields.update(over)
    return struct.pack(spec.JOURNAL_FMT, *(fields[f] for f in spec.JOURNAL_FIELDS))


def make_page(header: bytes | None, n_records: int, journal: list[bytes] | None = None,
              **rec_over: int) -> bytes:
    """Opened pages default to the mandatory J0 (op_count == the header's), exactly
    as page-open stamps it; pass journal=[] to build a benignly-impossible page."""
    if journal is None:
        if header is None:
            journal = []
        else:
            fields = dict(zip(spec.HEADER_FIELDS, struct.unpack(spec.HEADER_FMT, header)))
            journal = [make_journal_entry(op_count=fields["op_count"])]
    jbody = (b"".join(journal)
             + spec.BLANK_JOURNAL_ENTRY * (spec.JOURNAL_SLOTS - len(journal)))
    body = b"".join(make_record(t, **rec_over) for t in range(n_records))
    page = ((header if header is not None else spec.BLANK_HEADER) + jbody + body
            + spec.BLANK_RECORD * (spec.RECORDS_PER_PAGE - n_records))
    assert len(page) == spec.PAGE_SIZE
    return page


def region(page0: bytes, page1: bytes) -> bytes:
    return page0 + page1


# ---- dataset: flatten order --------------------------------------------------------

def test_flatten_is_feature_major() -> None:
    feats = extract_features(synthetic_nv_region())
    x = flatten_features(feats)
    assert x.shape == (N_DIMS,)
    # Contract: [all 40 mfcc, all 40 mel, all 40 chroma] -- the on-chip fill order.
    np.testing.assert_array_equal(x[:40], feats[:, 0])
    np.testing.assert_array_equal(x[40:80], feats[:, 1])
    np.testing.assert_array_equal(x[80:], feats[:, 2])


def test_flatten_rejects_wrong_shape() -> None:
    with pytest.raises(ValueError, match="expected"):
        flatten_features(np.zeros((3, 40), dtype=np.float32))


# ---- dataset: fill states ----------------------------------------------------------

def test_fill_states_walk_the_ring_lifecycle() -> None:
    blank = bytes([spec.ERASED_BYTE]) * spec.PAGE_SIZE
    cases = [
        (region(blank, blank), "empty"),
        (region(make_page(make_header(page_seq=1), 10), blank), "near-empty"),
        (region(make_page(make_header(page_seq=1), spec.RECORDS_PER_PAGE),
                make_page(make_header(page_seq=2), 40)), "pre-wrap"),
        (region(make_page(make_header(page_seq=3), 5),
                make_page(make_header(page_seq=2), spec.RECORDS_PER_PAGE)), "just-wrapped"),
        (region(make_page(make_header(page_seq=8), spec.RECORDS_PER_PAGE),
                make_page(make_header(page_seq=9), 60)), "steady"),
    ]
    for nv, expected in cases:
        assert fill_state(parse_region(nv)) == expected


def test_benign_structure_rejects_foreign_page() -> None:
    foreign = struct.pack("<Q", 1) + b"\xff" * (spec.PAGE_SIZE - 8)
    view = parse_region(region(make_page(make_header(), 3), foreign))
    with pytest.raises(ValueError, match="FOREIGN"):
        check_benign_structure(view, "x.bin")


def test_benign_structure_rejects_dirty_tail() -> None:
    blank = bytes([spec.ERASED_BYTE]) * spec.PAGE_SIZE
    page = bytearray(make_page(make_header(), 1))
    off = spec.RECORDS_OFFSET + 3 * spec.RECORD_SIZE   # plant a record past the blank head
    page[off : off + spec.RECORD_SIZE] = make_record(9)
    with pytest.raises(ValueError, match="dirty tail"):
        check_benign_structure(parse_region(region(bytes(page), blank)), "x.bin")


def test_benign_structure_rejects_dirty_header_pad() -> None:
    # The firmware programs the header's 12 reserve bytes as 0x00; anything else
    # means the header was rewritten or planted.
    blank = bytes([spec.ERASED_BYTE]) * spec.PAGE_SIZE
    header = bytearray(make_header())
    header[-1] = 0xAB                               # last pad byte
    with pytest.raises(ValueError, match="pad"):
        check_benign_structure(parse_region(region(make_page(bytes(header), 3), blank)),
                               "x.bin")


# The cross-page ring invariants: a page opens only when its predecessor is FULL,
# stamping seq+1 and op_count += RECORDS_PER_PAGE. Any other pair of valid headers
# is a state the logger cannot write -- the gate must refuse it.

def test_benign_structure_rejects_equal_page_seqs() -> None:
    view = parse_region(region(make_page(make_header(page_seq=2), spec.RECORDS_PER_PAGE),
                               make_page(make_header(page_seq=2), 5)))
    with pytest.raises(ValueError, match="corrupt ring"):
        check_benign_structure(view, "x.bin")


def test_benign_structure_rejects_nonconsecutive_seqs() -> None:
    view = parse_region(region(
        make_page(make_header(page_seq=1, op_count=0), spec.RECORDS_PER_PAGE),
        make_page(make_header(page_seq=3, op_count=spec.RECORDS_PER_PAGE), 5)))
    with pytest.raises(ValueError, match="consecutive"):
        check_benign_structure(view, "x.bin")


def test_benign_structure_rejects_older_page_not_full() -> None:
    view = parse_region(region(
        make_page(make_header(page_seq=1, op_count=0), 50),
        make_page(make_header(page_seq=2, op_count=spec.RECORDS_PER_PAGE), 5)))
    with pytest.raises(ValueError, match="predecessor is full"):
        check_benign_structure(view, "x.bin")


def test_benign_structure_rejects_broken_op_count_chain() -> None:
    view = parse_region(region(
        make_page(make_header(page_seq=1, op_count=0), spec.RECORDS_PER_PAGE),
        make_page(make_header(page_seq=2, op_count=100), 5)))
    with pytest.raises(ValueError, match="op_count chain"):
        check_benign_structure(view, "x.bin")


# The settings-journal invariants (spec v2): every opened page carries J0, the
# chain is contiguous and well-formed, and the entry op_counts tie the chain to
# the header and to the page's records.

def _one_page_region(page: bytes) -> bytes:
    return region(page, bytes([spec.ERASED_BYTE]) * spec.PAGE_SIZE)


def test_benign_structure_rejects_missing_j0() -> None:
    view = parse_region(_one_page_region(make_page(make_header(), 3, journal=[])))
    with pytest.raises(ValueError, match="J0"):
        check_benign_structure(view, "x.bin")


def test_benign_structure_rejects_journal_gap() -> None:
    page = bytearray(make_page(make_header(), 3))
    off = spec.JOURNAL_OFFSET + 2 * spec.JOURNAL_ENTRY_SIZE
    page[off : off + spec.JOURNAL_ENTRY_SIZE] = make_journal_entry(op_count=2)
    view = parse_region(_one_page_region(bytes(page)))
    with pytest.raises(ValueError, match="after a blank"):
        check_benign_structure(view, "x.bin")


def test_benign_structure_rejects_nonzero_reserved0() -> None:
    bad = [make_journal_entry(reserved0=0xBEEF)]
    view = parse_region(_one_page_region(make_page(make_header(), 3, journal=bad)))
    with pytest.raises(ValueError, match="reserved0"):
        check_benign_structure(view, "x.bin")


def test_benign_structure_rejects_out_of_range_units() -> None:
    bad = [make_journal_entry(unit_temp=2)]
    view = parse_region(_one_page_region(make_page(make_header(), 3, journal=bad)))
    with pytest.raises(ValueError, match="units"):
        check_benign_structure(view, "x.bin")


def test_benign_structure_rejects_j0_op_count_mismatch() -> None:
    bad = [make_journal_entry(op_count=1)]   # header op_count is 0
    view = parse_region(_one_page_region(make_page(make_header(), 3, journal=bad)))
    with pytest.raises(ValueError, match="J0 op_count"):
        check_benign_structure(view, "x.bin")


def test_benign_structure_rejects_decreasing_op_counts() -> None:
    bad = [make_journal_entry(op_count=0),
           make_journal_entry(unit_temp=1, op_count=2),
           make_journal_entry(op_count=1)]
    view = parse_region(_one_page_region(make_page(make_header(), 3, journal=bad)))
    with pytest.raises(ValueError, match="decrease"):
        check_benign_structure(view, "x.bin")


def test_benign_structure_rejects_op_count_beyond_records() -> None:
    bad = [make_journal_entry(op_count=0),
           make_journal_entry(unit_press=1, op_count=9)]   # only 3 records on the page
    view = parse_region(_one_page_region(make_page(make_header(), 3, journal=bad)))
    with pytest.raises(ValueError, match="record window"):
        check_benign_structure(view, "x.bin")


def test_settings_state_detects_change_entries() -> None:
    # J0 alone (the page-open stamp) is quiet; any entry beyond it is a runtime
    # settings change. The golden fixture carries one by design.
    quiet = region(make_page(make_header(page_seq=1), 3),
                   bytes([spec.ERASED_BYTE]) * spec.PAGE_SIZE)
    assert settings_state(parse_region(quiet)) == "settings-quiet"
    assert settings_state(parse_region(synthetic_nv_region())) == "settings-changed"
    blank = bytes([spec.ERASED_BYTE]) * spec.REGION_SIZE
    assert settings_state(parse_region(blank)) == "settings-quiet"


def test_benign_structure_accepts_equal_op_counts() -> None:
    # The FP-trap regression: two presses inside one record period stamp
    # EQUAL op_counts -- benign, so the gate must accept it ("strictly
    # increasing" was the audit-caught wrong rule).
    ok = [make_journal_entry(op_count=0),
          make_journal_entry(unit_temp=1, op_count=2),
          make_journal_entry(unit_temp=0, op_count=2)]
    view = parse_region(_one_page_region(make_page(make_header(), 3, journal=ok)))
    check_benign_structure(view, "x.bin")


def test_benign_structure_accepts_the_golden_fixture() -> None:
    # The spec-conformant fixture must clear every gate, or the gates are wrong.
    check_benign_structure(parse_region(synthetic_nv_region()), "fixture")


# ---- dataset: manifest-driven assembly ---------------------------------------------

def _write_capture(dir_: Path, name: str, nv: bytes) -> str:
    """Write a synthetic 256 KB dump embedding nv; return its REAL md5 hexdigest --
    the read path re-verifies manifest fingerprints, so records for on-disk files
    must carry the true digest."""
    dump = bytearray(DUMP_SIZE)          # zeros stand in for the static image
    dump[spec.DUMP_OFFSET:] = nv
    (dir_ / name).write_bytes(bytes(dump))
    return hashlib.md5(bytes(dump)).hexdigest()


def _rec(name: str, variant: str, label: str = "benign",
         md5: str = "a" * 32) -> DumpRecord:
    # The "a"*32 default is only for in-memory Samples that never touch a file;
    # any record load_samples will READ needs _write_capture's real digest.
    return DumpRecord(file=name, label=label, testbed="tbA", capture_point="boot-window",
                      mem_range="0x08040000-0x0807FFFF", md5=md5,
                      ts="2026-07-03T12:00:00", n_bytes=DUMP_SIZE,
                      conditions={"variant": variant})


def test_load_samples_filters_and_excludes(tmp_path: Path) -> None:
    md5s = {name: _write_capture(tmp_path, name, synthetic_nv_region())
            for name in ("a.bin", "b.bin", "c.bin")}
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest, [_rec("a.bin", "campaign", md5=md5s["a.bin"]),
                              _rec("b.bin", "campaign", md5=md5s["b.bin"]),
                              _rec("c.bin", "smoke", md5=md5s["c.bin"])])

    samples = load_samples(manifest, ("campaign",), quarantine=frozenset())
    assert [s.record.file for s in samples] == ["a.bin", "b.bin"]
    assert all(s.fill_state == "just-wrapped" for s in samples)   # fixture: seq 3/4
    assert all(s.range_ok and s.x.shape == (N_DIMS,) for s in samples)
    assert all(s.n_torn == 0 for s in samples)

    held = load_samples(manifest, ("campaign",), exclude=frozenset({"a.bin"}),
                        quarantine=frozenset())
    assert [s.record.file for s in held] == ["b.bin"]


def test_load_samples_skips_unrelated_missing_file(tmp_path: Path) -> None:
    # Records are filtered BEFORE bytes load: a deleted smoke capture (or any file
    # outside the requested variants) must not break assembly of the campaign.
    md5 = _write_capture(tmp_path, "a.bin", synthetic_nv_region())
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest, [_rec("a.bin", "campaign", md5=md5),
                              _rec("gone.bin", "smoke")])   # never written to disk
    samples = load_samples(manifest, ("campaign",), quarantine=frozenset())
    assert [s.record.file for s in samples] == ["a.bin"]


def test_load_samples_rejects_md5_mismatch(tmp_path: Path) -> None:
    # A capture corrupted/overwritten after capture time must fail loudly, not train.
    _write_capture(tmp_path, "a.bin", synthetic_nv_region())
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest, [_rec("a.bin", "campaign", md5="b" * 32)])  # wrong digest
    with pytest.raises(ValueError, match="md5"):
        load_samples(manifest, ("campaign",), quarantine=frozenset())


def test_load_samples_aborts_on_foreign_page_via_manifest(tmp_path: Path) -> None:
    foreign = struct.pack("<Q", 1) + b"\xff" * (spec.PAGE_SIZE - 8)
    nv = region(make_page(make_header(), 3), foreign)
    md5 = _write_capture(tmp_path, "bad.bin", nv)
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest, [_rec("bad.bin", "campaign", md5=md5)])
    with pytest.raises(ValueError, match="FOREIGN"):
        load_samples(manifest, ("campaign",), quarantine=frozenset())


def test_load_samples_refuses_spec_v1_capture(tmp_path: Path) -> None:
    # The rehearsal fence: a banked v1 capture (version 1 in its header bytes)
    # must be refused BY NAME, not spill a generic FOREIGN abort.
    nv = region(make_page(make_header(version=1), 3),
                bytes([spec.ERASED_BYTE]) * spec.PAGE_SIZE)
    md5 = _write_capture(tmp_path, "old.bin", nv)
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest, [_rec("old.bin", "campaign", md5=md5)])
    with pytest.raises(ValueError, match="spec v1"):
        load_samples(manifest, ("campaign",), quarantine=frozenset())


def test_load_samples_honors_quarantine(tmp_path: Path) -> None:
    # The designed recovery from a structurally-bad capture: quarantining its name
    # unblocks every future assembly run without touching the append-only manifest.
    foreign = struct.pack("<Q", 1) + b"\xff" * (spec.PAGE_SIZE - 8)
    bad_md5 = _write_capture(tmp_path, "bad.bin", region(make_page(make_header(), 3), foreign))
    good_md5 = _write_capture(tmp_path, "good.bin", synthetic_nv_region())
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest, [_rec("bad.bin", "campaign", md5=bad_md5),
                              _rec("good.bin", "campaign", md5=good_md5)])
    samples = load_samples(manifest, ("campaign",), quarantine=frozenset({"bad.bin"}))
    assert [s.record.file for s in samples] == ["good.bin"]


def test_load_samples_flags_out_of_range(tmp_path: Path) -> None:
    nv = region(make_page(make_header(page_seq=1), 3, press=0), bytes([0xFF]) * spec.PAGE_SIZE)
    md5 = _write_capture(tmp_path, "bad.bin", nv)
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest, [_rec("bad.bin", "campaign", md5=md5)])
    (sample,) = load_samples(manifest, ("campaign",), quarantine=frozenset())
    assert sample.range_ok is False       # press=0 < legal 30000: marked, not rejected
    assert sample.n_torn == 0             # a real range bug, not a torn write


def test_dataset_cli_warns_on_duplicate_md5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # Byte-identical duplicates double-weight training and can leak the exam
    # (the v1 dry run found real ones); the sanity CLI warns, never aborts --
    # two legitimately identical captures must not become a false-positive trap.
    md5_a = _write_capture(tmp_path, "a.bin", synthetic_nv_region())
    md5_b = _write_capture(tmp_path, "b.bin", synthetic_nv_region())
    assert md5_a == md5_b
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest, [_rec("a.bin", "campaign", md5=md5_a),
                              _rec("b.bin", "campaign", md5=md5_b)])
    monkeypatch.setattr(dataset_mod, "DEFAULT_MANIFEST", manifest)
    assert dataset_mod.main(["campaign"]) == 0
    out = capsys.readouterr().out
    assert "duplicate" in out and "a.bin" in out and "b.bin" in out


def test_load_samples_counts_torn_records(tmp_path: Path) -> None:
    # A reset inside the record program leaves the second doubleword erased:
    # hum+press read 0xFFFFFFFF. Marked as torn (and out of range), never repaired.
    erased = 0xFFFFFFFF
    body = (make_record(0) + make_record(45, hum=erased, press=erased) + make_record(90))
    page = (make_header(page_seq=1)
            + make_journal_entry() + spec.BLANK_JOURNAL_ENTRY * (spec.JOURNAL_SLOTS - 1)
            + body
            + spec.BLANK_RECORD * (spec.RECORDS_PER_PAGE - 3))
    nv = region(page, bytes([0xFF]) * spec.PAGE_SIZE)
    md5 = _write_capture(tmp_path, "torn.bin", nv)
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest, [_rec("torn.bin", "campaign", md5=md5)])
    (sample,) = load_samples(manifest, ("campaign",), quarantine=frozenset())
    assert sample.n_torn == 1
    assert sample.range_ok is False


# ---- split -------------------------------------------------------------------------

def _fake_samples(states: dict[str, int], settings: str = "settings-quiet",
                  prefix: str = "cap") -> list[Sample]:
    out: list[Sample] = []
    i = 0
    for state, n in states.items():
        for _ in range(n):
            out.append(Sample(record=_rec(f"{prefix}{i:03d}.bin", "campaign"),
                              x=np.zeros(N_DIMS, dtype=np.float32),
                              fill_state=state, settings_state=settings,
                              n_records=0, n_torn=0, range_ok=True))
            i += 1
    return out


def test_choose_holdout_stratified_and_deterministic() -> None:
    samples = _fake_samples({"near-empty": 5, "steady": 20, "empty": 1})
    chosen, notes = choose_holdout(samples, fraction=0.2, seed=7)
    by_state: dict[str, int] = {}
    for s in chosen:
        by_state[s.fill_state] = by_state.get(s.fill_state, 0) + 1
    assert by_state == {"near-empty": 1, "steady": 4}   # round(0.2*5)=1, round(0.2*20)=4
    assert "empty" not in by_state                       # singleton stays in training
    assert any("only 1" in n for n in notes)
    again, _ = choose_holdout(samples, fraction=0.2, seed=7)
    assert [s.record.file for s in again] == [s.record.file for s in chosen]


def test_choose_holdout_stratifies_settings_state() -> None:
    # Captures with journal change entries are their own stratum: the exam holds
    # some out while training keeps the rest -- neither side may go uncovered.
    samples = (_fake_samples({"steady": 8}, prefix="q")
               + _fake_samples({"steady": 4}, settings="settings-changed", prefix="c"))
    chosen, _ = choose_holdout(samples, fraction=0.25, seed=11)
    by_stratum: dict[str, int] = {}
    for s in chosen:
        key = f"{s.fill_state}/{s.settings_state}"
        by_stratum[key] = by_stratum.get(key, 0) + 1
    assert by_stratum == {"steady/settings-quiet": 2, "steady/settings-changed": 1}


def test_choose_holdout_never_empties_a_stratum() -> None:
    chosen, _ = choose_holdout(_fake_samples({"steady": 2}), fraction=0.9, seed=1)
    assert len(chosen) == 1               # capped at n-1: training keeps one


def test_holdout_file_round_trip(tmp_path: Path) -> None:
    samples = _fake_samples({"steady": 4})
    chosen, notes = choose_holdout(samples, fraction=0.5, seed=3)
    path = tmp_path / "holdout.txt"
    write_holdout(path, chosen, ("campaign", "extra"), 0.5, 3, len(samples), notes)
    assert read_holdout(path) == {s.record.file for s in chosen}
    # The variants header is machine-read back: fit.py refuses to train on variants
    # the split never saw (they would carry zero exam coverage).
    assert read_holdout_variants(path) == frozenset({"campaign", "extra"})


def test_holdout_without_variants_header_reads_none(tmp_path: Path) -> None:
    path = tmp_path / "holdout.txt"
    path.write_text("# a pre-scope-check list\nx.bin\n", encoding="utf-8")
    assert read_holdout(path) == frozenset({"x.bin"})
    assert read_holdout_variants(path) is None


# ---- fit / score / threshold / artifact --------------------------------------------

def test_distance_arithmetic_exact() -> None:
    # mean 0, precision I: d(x) is the plain euclidean norm -- checks the formula.
    mean = np.zeros(3)
    precision = np.eye(3)
    x = np.array([[3.0, 4.0, 0.0], [0.0, 0.0, 0.0]])
    np.testing.assert_allclose(distances(mean, precision, x), [5.0, 0.0])
    # A non-trivial precision, by hand: d^2 = 2*1 + 0.5*4 = 4.
    precision = np.diag([2.0, 0.5, 1.0])
    np.testing.assert_allclose(distances(mean, precision, np.array([[1.0, 2.0, 0.0]])),
                               [2.0])


def test_fit_recovers_a_known_gaussian() -> None:
    rng = np.random.default_rng(0)
    mu = np.array([10.0, -5.0, 0.0, 3.0])
    x = rng.normal(mu, 1.0, size=(400, 4))
    mean, precision, shrinkage = fit_mean_precision(x)
    np.testing.assert_allclose(mean, mu, atol=0.2)
    assert 0.0 <= shrinkage <= 1.0
    assert distances(mean, precision, mu[None, :])[0] < 0.5   # the true center scores ~0


def test_fit_rejects_constant_data() -> None:
    with pytest.raises(ValueError, match="constant"):
        fit_mean_precision(np.ones((10, 4)))


def test_fit_is_scale_invariant() -> None:
    # Feature columns live on wildly different rulers (MFCC ~1e2, mel power ~1e-6).
    # The standardized fit must score a same-sized deviation (in units of that
    # column's own training spread) identically whatever the ruler -- the raw-scale
    # Ledoit-Wolf fit fails this by silencing the small-scale columns.
    rng = np.random.default_rng(3)
    x = rng.normal(0.0, 1.0, size=(40, 3))
    scales = np.array([1.0, 1e-6, 1e4])
    mean_a, prec_a, _ = fit_mean_precision(x)
    mean_b, prec_b, _ = fit_mean_precision(x * scales)
    probe = np.array([[2.0, -1.0, 0.5]])
    np.testing.assert_allclose(distances(mean_b, prec_b, probe * scales),
                               distances(mean_a, prec_a, probe), rtol=1e-8)


def test_loo_flags_an_outlier() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(0.0, 1.0, size=(20, 4))
    x[7] += 25.0                          # one sample far off the benign cloud
    loo = loo_distances(x)
    assert loo.shape == (20,) and np.isfinite(loo).all()
    others = np.delete(loo, 7)
    assert loo[7] > 3 * others.max()      # scored WITHOUT itself, it stands out


def test_threshold_is_the_higher_order_stat() -> None:
    loo = np.arange(1.0, 11.0)            # 1..10
    assert threshold_from_loo(loo, 0.20) == 9.0
    assert threshold_from_loo(loo, 0.01) == 10.0   # finer than 1/n -> the max
    with pytest.raises(ValueError, match="fp_target"):
        threshold_from_loo(loo, 0.0)


def test_artifact_round_trip(tmp_path: Path) -> None:
    rng = np.random.default_rng(2)
    model = MahalanobisModel(mean=rng.normal(size=5), precision=np.eye(5),
                             threshold=4.25, meta={"n_train": 9, "variants": ["t"]})
    npz_path, json_path = save_model(model, tmp_path / "m")
    assert npz_path.exists() and json_path.exists()
    back = load_model(npz_path)
    np.testing.assert_array_equal(back.mean, model.mean)
    np.testing.assert_array_equal(back.precision, model.precision)
    assert back.threshold == 4.25 and back.meta["n_train"] == 9
    # threshold None survives the NaN encoding
    no_thr = MahalanobisModel(model.mean, model.precision, None, {"n_train": 9})
    save_model(no_thr, tmp_path / "n")
    assert load_model(tmp_path / "n").threshold is None


# ---- score: dispatch + verdict path --------------------------------------------------

def _dump_with(nv: bytes) -> bytes:
    dump = bytearray(DUMP_SIZE)
    dump[spec.DUMP_OFFSET:] = nv
    return bytes(dump)


def test_score_bytes_accepts_slice_and_full_dump() -> None:
    # A bare 4 KB NV slice and the 256 KB dump embedding it must score identically.
    nv = synthetic_nv_region()
    rng = np.random.default_rng(5)
    model = MahalanobisModel(mean=rng.normal(size=N_DIMS),
                             precision=np.eye(N_DIMS), threshold=None, meta={})
    assert score.score_bytes(model, nv) == score.score_bytes(model, _dump_with(nv))


def test_score_main_verdict_and_size_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    dump = _dump_with(synthetic_nv_region())
    bin_path = tmp_path / "cap.bin"
    bin_path.write_bytes(dump)
    rng = np.random.default_rng(6)
    model = MahalanobisModel(mean=rng.normal(size=N_DIMS),
                             precision=np.eye(N_DIMS), threshold=None, meta={})
    d = score.score_bytes(model, dump)
    assert d > 0.0
    # Threshold below the capture's distance: the verdict line must say ANOMALY.
    npz_path, _ = save_model(
        MahalanobisModel(model.mean, model.precision, d / 2, {}), tmp_path / "m")
    monkeypatch.setattr(sys, "argv", ["score", str(npz_path), str(bin_path)])
    assert score.main() == 0
    out = capsys.readouterr().out
    assert "ANOMALY" in out and "1 of 1 flagged" in out

    # A wrong-size file is refused with a message, not a raw traceback.
    short = tmp_path / "short.bin"
    short.write_bytes(b"\x00" * 100)
    monkeypatch.setattr(sys, "argv", ["score", str(npz_path), str(short)])
    assert score.main() == 1
    assert "expected" in capsys.readouterr().out
