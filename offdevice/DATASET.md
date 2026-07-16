# Dataset & feature-contract provenance

This is the committed, current-state record of what pins the exact feature numbers and
what the benign dataset is: the resolved library versions, the golden reference vector,
the capture campaign's design, the policies (holdout, quarantine, designed misses)
that keep the methodology honest, and the fitted model + threshold those captures
produced. It describes the present state only — its history, like every past state of
the dataset design, lives in git.

Spec V1 is what our 4KB NS flash region looked like before adding a settings section. Spec V2 is the after.


## Environment pins

librosa, numpy, and scipy resolve the exact feature values, so the resolved versions are
part of the contract the on-chip CMSIS-DSP port targets. After (re)building the venv,
record the output of:

```powershell
pip freeze | Select-String -Pattern "librosa|numpy|scipy|scikit-learn|soundfile|pyserial"
```

Current pins: librosa==0.10.2.post1, numpy==2.0.2, scipy==1.17.1, scikit-learn==1.9.0,
soundfile==0.14.0, pyserial==3.5 (Python 3.12.10).

## The golden vector (the frozen feature contract)

`offdevice/tests/golden/synthetic_features.npy` is the frozen reference output of the
feature pipeline for a fixed synthetic input. It pins the exact numbers the pipeline
emits, so any drift (a library bump, a refactor, an accidental parameter edit) becomes a
loud test failure instead of silent feature corruption, and it is the concrete reference
the on-chip CMSIS-DSP port is validated against.

- The current golden takes a spec-v2-conformant synthetic 4 KB NV image
  (`fixtures.synthetic_nv_region`: page headers, settings-journal chains — J0 per page
  plus one mid-page unit change — and triangle-wave records) through the pipeline
  frozen in `offdevice/features/params.py`: one byte = one sample, `n_fft=512`,
  `hop_length=128` (33 frames over 4096 samples), with MFCC's internal mel filterbank
  pinned to 40 bands and fmax 8000 so it is identical to the standalone mel feature's
  bank and the on-chip port computes one filterbank.
- Every regeneration (`python -m offdevice.tests.make_golden`) is deliberate. It happens
  only when a feature-affecting change is intended, and this section is updated in the
  same commit. The latest re-freeze accompanied the deploy-rate change from 45 s to 15 s
  (the fixture's record timestamps step at the deploy rate, so the fixture bytes moved);
  the feature mathematics did not change. The re-freeze before that accompanied the
  spec-v2 fixture layout (the settings journal moved the record grid).



## The benign dataset — current state

- One capture = one 4 KB NV snapshot = one training sample. Captures travel as 256 KB
  whole-flash dumps on the wire, and the NV slice is cut out in Python.
- **The dataset is the combined spec-v2 + BME280 campaign: 153 distinct benign
  captures, committed in `offdevice/data/captures/` together with `manifest.jsonl`.**
  The eight campaign tags are `nv15s-lab-{fill1,fill2,steady1,top1,top2,top3}` and
  `nv15s-lab2-{steady1,top1}`. Fill-state strata: empty=1, near-empty=3, pre-wrap=6,
  just-wrapped=3, steady=140; settings strata: settings-quiet=98, settings-changed=55.
  One further campaign capture — a byte-identical seam clone — is retracted via the
  quarantine list (policy below), giving 153 distinct from 154 banked.
- **Retired data lives outside the repo.** `offdevice/data/captures_retired/`
  (git-ignored) holds the spec-v1 rehearsal captures (all `nv45s-*` tags, taken with
  the retired dummy generator and unparseable under spec v2), the smoke/bench runs,
  the retracted clone, and the collection logs. The manifest keeps their entries —
  it is append-only history — while the spec-version fence and the tag filters keep
  them out of every model path.
- **The training rate is 15 s between records (the deploy preset), and it is the only
  training rate.** The ring fully turns over in about 61 minutes at that rate (so
  captures spaced an hour or more apart share no records), and the flash-wear budget
  holds for about 14 months. The rate was deliberately traded down from 45 s (which
  carried a ~3.5-year wear budget) so that a statistically sufficient campaign fits in
  days rather than weeks; the demo runtime uses the same 15 s rate, because the model
  must train on the distribution it will scan. The 1 s dev preset exists for bring-up
  only and never produces training data.
- **Collection is unattended** (`python -m offdevice.data.collect <tag>`): each cycle
  hardware-resets the board via ST-LINK and captures during the firmware's boot window
  (`DUMP_NSFLASH=2`), so every snapshot is a frozen, consistent ring, and every capture
  carries a benign reboot seam (a timestamp restart).
- **Fill-state coverage:** the strata derive from the parsed ring. "Near-empty" means at
  most half a page of records (61 of the 122-record page). A
  fresh campaign starts with a short-interval fill pass that walks
  empty → near-empty → pre-wrap → just-wrapped, then accumulates steady-state samples on
  the long interval.
- **Settings coverage (spec v2):** campaign builds carry a compile-time flag that
  toggles the display units on a bounded, rule-governed schedule, so pages with journal
  change entries appear in the training set in realistic proportion (most pages carry
  only the page-open entry J0, and a minority carry one to three changes). The deploy
  build never contains the flag.
- **An erased ring is benign BY DESIGN** (a deliberate decision, not an accident of the
  schedule): fresh-start campaigns put all-0xFF "empty" captures in training, so the
  model learns an erased NV region as normal, and the parser treats blank pages as
  legal. This is consistent with the threat model — the IDS hunts a foreign payload
  *hidden* in NV, and an erased region hides nothing.
- **Variant tags:** `nv15s-<run-id>` marks final-campaign data (the 15 s prefix also
  keeps every dead spec-v1 `nv45s-*` rehearsal tag visibly distinct); smoke and bench
  tags (`nvdev-*`, `*-smoke`) are plumbing verification, excluded from training.



## Holdout policy

About 20% of the captures are chosen — stratified across ring fill states and, under
spec v2, settings states — BEFORE any model fitting, and are held out for the
false-positive sanity check. They never touch the fit or the threshold. The mechanism:
`python -m offdevice.model.split <tags>` derives each capture's strata from its parsed
ring and writes the chosen filenames to `offdevice/data/holdout.txt`, which is
**committed** as the audit trail that the exam set was locked away before any fitting.
The eight collaborator anomaly-base captures (recorded with their as-mailed md5s in
the committed `offdevice/data/collab_bases.txt`) are **pinned into the holdout** via
the split's `--pin` option: every anomaly the collaborator returns is a tampered copy
of one of these bases, so the model must never have trained on the file under the
tampering, or the detection numbers would carry a memorized-base confound. The split
verifies each pinned base's manifest md5 against its as-mailed md5 before locking the
list — the file being tampered is provably the file excluded from training.
holdout.txt also records which variants the split saw (the `# variants:` header), and
`fit.py` refuses variants beyond that set, because their captures would train with zero
exam coverage. `fit.py` also refuses to run without the file (or an explicit
`--no-holdout` for plumbing checks). The held-out captures are scored exactly once,
after the threshold is chosen, via `offdevice.model.score`.



## Quarantine policy (retracting a bad capture)

The manifest is append-only and is never edited. If a capture turns out to be
structurally bad (for example a foreign page — the benign gate in
`offdevice/model/dataset.py` aborts on it), the designed exit is
`offdevice/data/quarantine.txt`: one bare filename per line, with the reason after `#`,
in the same format as holdout.txt. Every model path (dataset assembly, split, fit,
holdout scoring) skips quarantined names, and a name must never sit in both quarantine
and holdout (fit and score refuse). The file is committed for the same auditability
reason as holdout.txt — it changes what the model trained on. Its one current entry
retracts a byte-identical campaign clone: two captures taken across a chain seam with
no record written between them produced the same 256 KB bytes, and a duplicate would
double-weight training. (A missing file simply means no retractions.)



## The fitted model — current state

The shipped one-class model is a Mahalanobis detector fitted on the 122 training
captures (the 153 minus the 31-capture holdout): the benign mean plus a
Ledoit-Wolf-shrunk precision matrix (shrinkage 0.048) over the 120-dimension feature
vector. The artifact is `offdevice/model/artifacts/mahalanobis.npz` with a
human-readable `.json` sidecar (both committed; the sidecar records full provenance,
including every leave-one-out distance by capture name). The firmware constants are
the generated `engine/nv_model_params.h` + `nv_model_testvec.h` — the artifact's
SHA-256 is stamped in the header, and the hand-written C scorer reproduces the
Python verdicts on both sides of the alarm line (host parity PASS; the test vectors
bracket the threshold at 0.99x and 1.01x).

- **Threshold: 13.874, set at 2% benign false-positive target.** It
  is the 98th-percentile of the 122 leave-one-out distances — each capture scored by
  a model fitted without it; in-sample distances never set thresholds. The
  leave-one-out distribution: min 4.06, median 7.43, p90 10.19, p95 11.48, max 16.36.
- **The benign distribution is unimodal** — no fill-state or settings-state lumps, so
  the single-Gaussian model stands and the Gaussian-mixture escalation was not
  needed. Its heavy tail is the benign ring-cycle gradient: near-full rings (238–244
  records) and just-rotated rings score systematically higher, a continuous and
  legitimate slope, not a separate mode.
- **Holdout exam, scored exactly once: 1 of 31 flagged (3.2%), inside the
  pre-registered 0–2 consistency budget for a 2% target.** The single alarm is the
  fullest-possible ring (244 records, d=14.34 against the 13.874 line) — the top of
  the known benign gradient, and the capture named in advance as the likeliest alarm.
  Honest resolution: a 31-capture exam resolves false-positive rates only down to
  about 1-in-31 (~3%); the 2% figure rests on the leave-one-out quantile, whose
  resolution is 1/122.



## Designed misses (stated expectations for the anomaly eval)

These are anomaly classes the detector is NOT supposed to catch. The eval reports them
separately so the detection curve stays honest.

- **Whole-region erase.** An erased ring is a legitimate state (see above); a region
  erase is an availability nuisance the logger recovers from, not hidden persistence.
- **Small in-range value tweaks.** Nudging a reading that stays inside its legal range
  is value-level substitution, and value-level safety is the firmware's job, not the
  detector's.
- **The perfect settings mimic (spec v2).** An attacker who programs the next free
  journal slot with legal values and a plausible op_count is byte-identical to a real
  button press, and is therefore undetectable by construction.
- **The defaults downgrade (spec v2).** One corrupted byte in the chain-end journal
  entry makes the next boot fall back to °C/hPa: the firmware behaves safely, the
  display units silently revert, and an 8-byte tamper of that size is likely below the
  spectral features' detection floor.
- **Whole-flash wipe.** Tampering the other 252 KB of flash is the static hash's catch,
  not this detector's.
