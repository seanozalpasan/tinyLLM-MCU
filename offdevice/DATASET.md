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
- **The modeled dataset is the combined spec-v2 + BME280 campaign plus the honest
  late-fill (tail) collections: 194 benign captures, committed in
  `offdevice/data/captures/` together with `manifest.jsonl`.** The modeled tags are
  `nv15s-lab-{fill1,fill2,steady1,top1,top2,top3}`, `nv15s-lab2-{steady1,top1}`,
  `nv15s-lab-tail2` (8), `nv15s-lab-tail3-withsettings` (3), and `nv15s-lab-tail4`
  (30: 29 honest tails plus 1 positioning capture). Fill-state strata: empty=1,
  near-empty=3, pre-wrap=6, just-wrapped=3, steady=181; settings strata:
  settings-quiet=137, settings-changed=57. One further campaign capture — a
  byte-identical seam clone — is retracted via the quarantine list (policy below).
- **The `nv15s-lab-tail1` collection (69 captures) is banked but excluded from every
  model path** — the split's variant list simply omits the tag, so nothing trains or
  examines on it. Its burst collector captured every ~45 s through the late-fill
  band, and because every capture resets the board, those captures' trailing records
  carry a repeating timestamp-restart lattice that a steadily running board never
  writes. A model trained on that texture learned it as the benign corner and then
  over-scored live monotonic corners — the direct cause of the false alarms that
  triggered the exclusion. The captures stay in the manifest as history.
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
- **The tail collections cover the near-full-page corner, and only honest texture
  trains.** The live benign score climbs as a page fills and peaks in the page's
  last ~20 records, and the original campaign's wide capture spacing sampled that
  corner too thinly to protect it. `nv15s-lab-tail2`, `nv15s-lab-tail3-withsettings`,
  and `nv15s-lab-tail4` (`offdevice/data/tail_honest.py`) capture once per page
  cycle with the IDS disarmed (`IDS_SCAN_ARMED 0`), so each banked tail is written
  in one unbroken run with monotonic timestamps, exactly as deployment writes it —
  verified per capture by the collector. tail4 is the deep-corner mass: 29 honest
  tails collected one per cycle over 14.5 hours, at rotating aim depths that landed
  write-head slots 115 through 121 (the alarm-crossing band itself), settings-quiet
  to match the deploy build. The excluded lattice-textured alternative is described
  above.
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
The pin list `offdevice/data/holdout_pins.txt` forces named captures into the holdout
via the split's `--pin` option, and it carries two groups. First, the eight
collaborator anomaly-base captures (as-mailed md5s repeated from the committed
`offdevice/data/collab_bases.txt`): every anomaly the collaborator returns is a
tampered copy of one of these bases, so the model must never have trained on the file
under the tampering, or the detection numbers would carry a memorized-base confound.
Second, 6 of the 29 tail4 honest tails, chosen by a rule fixed before any scoring
(every fifth honest tail by run number, starting at run001) so that every sampled
corner depth appears in the exam. Because the pinned tails are honest texture the
model never trained on, they answer the corner false-positive question directly —
an exam property the earlier tail1-era split could not honestly claim, since its
exam corners had near-twin captures in training and their clean scores were partly
twin-recognition. The split verifies each md5-carrying pin against the manifest
before locking the list — the file being tampered is provably the file excluded
from training.
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

The shipped one-class model is a Mahalanobis detector fitted on the 155 training
captures (the 194 minus the 39-capture holdout): the benign mean plus a
Ledoit-Wolf-shrunk precision matrix (shrinkage 0.034) over the 120-dimension feature
vector. The artifact is `offdevice/model/artifacts/mahalanobis.npz` with a
human-readable `.json` sidecar (both committed; the sidecar records full provenance,
including every leave-one-out distance by capture name). The firmware constants are
the generated `engine/nv_model_params.h` + `nv_model_testvec.h` — the artifact's
SHA-256 is stamped in the header, and the hand-written C scorer reproduces the
Python verdicts on both sides of the alarm line (host parity PASS; the test vectors
bracket the threshold at 0.99x and 1.01x).

- **Threshold: 13.909, set at the 1% benign false-positive target.** It is the
  99th-percentile of the 155 leave-one-out distances — each capture scored by a
  model fitted without it; in-sample distances never set thresholds. The
  leave-one-out distribution: min 4.093, median 7.607, p90 10.478, p95 11.651,
  max 15.484. The 1% target (over the earlier 2% default) was chosen on live
  benign evidence alone: on-board clean corner peaks run 12.5–13.1, above the 2%
  candidate line (12.632), and the threshold was fixed before any anomaly was
  scored against this model.
- **The benign distribution is unimodal** — no fill-state or settings-state lumps, so
  the single-Gaussian model stands and the Gaussian-mixture escalation was not
  needed. Its upper tail is the benign ring-cycle gradient: near-full rings and
  just-rotated rings score systematically higher, a continuous and legitimate slope,
  not a separate mode. The trained honest tails sit inside the envelope (all below
  12.4 in leave-one-out), and the envelope's edge is set by ordinary fullest-ring
  captures plus one recurring benign outlier (15.484).
- **Holdout exam, scored exactly once: 0 of 39 flagged**, inside the pre-registered
  0–1 budget for a 1% target on a 39-capture exam. The exam includes 12 tail4
  captures the model never trained on (11 deep-corner honest tails and the one
  positioning capture) — 6 pinned by the fixed rule plus 6 taken by the blind
  stratified draw — and they scored 5.8 to 12.1, a
  direct, twin-free answer to "does the model false-alarm on honest corners it
  has never seen?". Honest resolution: a 39-capture exam resolves false-positive
  rates only down to ~1-in-39 (~2.6%); the 1% figure rests on the leave-one-out
  quantile, whose resolution is 1/155.
- **The operating corridor is measured on both sides.** A 120-minute on-board soak
  (292 scans, four full corner passes and rotations, one unbroken run with zero
  reboots and zero alarms) put the live benign ceiling at 12.742, and a live
  settings-unit exercise (all four display-unit states toggled through a near-full
  page and a rotation) peaked at 12.364. The weakest caught anomaly, a 512 B
  foreign blob planted on the fullest-ring base, scores 14.284. The 13.909 line
  sits between them with ~1.2 of benign-side margin and 0.375 of detection-side
  margin; every other caught blob clears the line by 5.9 to 126. The
  512 B-blob-on-fullest-ring case is the measured detection floor and is reported
  as such, not averaged away.



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
