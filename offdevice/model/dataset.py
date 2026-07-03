"""
Benign-dataset assembly: manifest records -> model-ready feature vectors + fill states.

Sits between the capture toolchain (offdevice/data) and the one-class model
(offdevice/model/fit.py): filter the manifest to the requested benign campaign tags,
slice each capture's 4 KB NV region, run the frozen feature pipeline, and flatten each
(40, 3) matrix to the model's flat input vector. Each capture also gets its ring FILL
STATE (empty -> near-empty -> pre-wrap -> just-wrapped -> steady), derived from the
parsed page headers -- the collector can't record it (it never parses the ring), and
the holdout split stratifies on it.

Benign-strict: a structural layout violation (foreign page, dirty tail) in a capture
labeled benign aborts assembly -- that byte pattern is the very anomaly class the IDS
hunts, and it must never train the model silently. Out-of-range VALUES only mark the
sample (range_ok=False): they point at a generator bug, not tampering, and the caller
decides (fit.py refuses them by default).

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
from offdevice.data.loader import iter_dataset
from offdevice.features import params
from offdevice.features.extract import extract_features
from offdevice.nv import spec
from offdevice.nv.parse import RegionView, parse_region, records_chronological, slice_nv

DEFAULT_MANIFEST = DEFAULT_CAPTURES_DIR / MANIFEST_NAME

# The model's input dimension: the (40, 3) feature matrix flattened feature-major.
N_DIMS = params.N_BINS * params.N_FEATURES

# Ring fill states, the holdout-split strata: the benign byte regimes different enough
# to each need representation in both the training and the held-out exam set.
FILL_STATES = ("empty", "near-empty", "pre-wrap", "just-wrapped", "steady")


@dataclass(frozen=True)
class Sample:
    """One capture, model-ready: provenance + flat feature vector + ring context."""

    record: DumpRecord
    x: npt.NDArray[np.float32]   # (N_DIMS,) flat feature vector
    fill_state: str              # one of FILL_STATES
    n_records: int               # ring records present at capture time
    range_ok: bool               # every stored value inside its channel's legal range


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


def check_benign_structure(view: RegionView, name: str) -> None:
    """Abort on a structural layout violation in a benign-labeled capture.

    A foreign page (bytes without a valid header -- e.g. the old proof-demo counter
    doubleword) or a written slot after the head is exactly the structural corruption
    the detector exists to flag; training on it would teach the model that tampering
    is normal. Erase the NV pages / investigate the capture instead.
    """
    for i, page in enumerate(view.pages):
        if page.header is None and not page.blank:
            raise ValueError(f"{name}: page{i} has bytes but no valid header (FOREIGN)")
        if not page.tail_clean:
            raise ValueError(f"{name}: page{i} has a written slot after the head (dirty tail)")


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
    root: str | Path | None = None,
) -> list[Sample]:
    """Assemble model-ready samples for the given campaign tags.

    Keeps records with label "benign" and conditions["variant"] in `variants`;
    `exclude` (bare filenames -- the holdout list) is dropped after that filter.
    Order follows the manifest (append-only, so it is stable and chronological).
    """
    wanted = set(variants)
    samples: list[Sample] = []
    for record, raw in iter_dataset(manifest_path, root):
        if record.label != "benign" or record.conditions.get("variant") not in wanted:
            continue
        if Path(record.file).name in exclude:
            continue
        nv = slice_nv(raw)
        view = parse_region(nv)
        check_benign_structure(view, record.file)
        samples.append(Sample(
            record=record,
            x=flatten_features(extract_features(nv)),
            fill_state=fill_state(view),
            n_records=len(records_chronological(view)),
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
    samples = load_samples(DEFAULT_MANIFEST, tuple(argv))
    if not samples:
        print(f"no benign captures in {DEFAULT_MANIFEST} match variants {argv}")
        return 1

    counts: dict[str, int] = {}
    for s in samples:
        counts[s.fill_state] = counts.get(s.fill_state, 0) + 1
        nan = "NaN!" if np.isnan(s.x).any() else "ok"
        rng = "ok" if s.range_ok else "OUT-OF-RANGE"
        print(f"{s.record.file:60s} {s.fill_state:12s} recs={s.n_records:3d} "
              f"nan={nan:4s} range={rng}")
    state_summary = "  ".join(f"{st}={counts[st]}" for st in FILL_STATES if st in counts)
    print(f"\n{len(samples)} captures: {state_summary}")
    bad = sum(1 for s in samples if not s.range_ok)
    if bad:
        print(f"WARNING: {bad} capture(s) hold out-of-range values -- generator bug era? "
              f"fit.py refuses them unless --allow-out-of-range")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
