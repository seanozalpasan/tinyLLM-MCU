 # Dataset & feature-contract provenance

The committed record of everything that pins the feature numbers and the benign
dataset: resolved library versions, deliberate golden re-freezes, and the capture
campaign's design.

## Environment pins

librosa/numpy/scipy resolve the exact feature values, so the resolved versions are
part of the contract the on-chip CMSIS-DSP port targets. After (re)building the
venv, record the output of:

```powershell
pip freeze | Select-String -Pattern "librosa|numpy|scipy|scikit-learn|soundfile|pyserial"
```

Current pins: librosa==0.10.2.post1, numpy==2.0.2, scipy==1.17.1,
scikit-learn==1.9.0, soundfile==0.14.0, pyserial==3.5 (Python 3.12.10)

## Golden re-freeze log

The golden vector (`offdevice/tests/golden/synthetic_features.npy`) is the frozen
reference output of the feature pipeline for a fixed synthetic input. Every
regeneration is deliberate and gets a line here.

- **Re-tuned for the 4 KB NV window (256 KB golden retired).**
  The pipeline now takes exactly the 4 KB NV region, not the whole flash image.
  `n_fft` 2048 → 512, `hop_length` 512 → 128 (33 frames over 4096 samples);
  MFCC's internal mel filterbank pinned to 40 bands / fmax 8000 (librosa's
  128-band default has empty filters at n_fft=512, and 40/8000 makes it identical
  to the standalone mel feature's, so the on-chip port computes one filterbank).
  Golden input changed from 256 KB of seeded random bytes to a closed-form,
  spec-conformant 4 KB NV image (`fixtures.synthetic_nv_region`).

## Benign dataset capture

- **One capture = one 4 KB NV snapshot = one training sample.** Captures are
  256 KB whole-flash dumps on the wire; the NV slice is cut out in Python.
- **Rate: 45 s between records (deploy preset), the only training rate.** Each
  page erases every ~3.1 h → ~3.5 years to the ~10k-cycle rated wear; the ring
  fully turns over every ~3.1 h, so captures ≥3 h apart share no records
  (2 h spacing ≈ ⅓ overlap — accepted for yield). The 1 s dev preset never
  produces training data.
- **Collection is unattended** (`python -m offdevice.data.collect <tag>`): each
  cycle hardware-resets the board via ST-LINK and captures during the firmware's
  boot window (`DUMP_NSFLASH=2`), so every snapshot is a frozen, consistent ring
  and every capture leaves a benign reboot seam (timestamp restart) in the data.
- **Fill-state coverage:** runs started with `--fresh` (erased ring) walk
  near-empty → pre-wrap → just-wrapped → steady state over the first ~3.5 h;
  steady-state samples accumulate thereafter.
- **Variant tags:** `nv45s-<run-id>` for campaign data; `nv45s-smoke` (and any
  other non-campaign tag) is plumbing verification, excluded from training.
- **Holdout policy:** ~20% of captures, chosen stratified across fill states
  BEFORE any model fitting, held out for false-positive sanity checks; they never
  touch the fit or the threshold. Mechanism: `python -m offdevice.model.split
  <tags>` derives each capture's fill state from its parsed ring and writes the
  chosen filenames to `offdevice/data/holdout.txt`, which is **committed** — the
  audit trail that the exam set was locked away before any fitting. `fit.py`
  refuses to run without it (or an explicit `--no-holdout` for plumbing checks);
  the held-out captures are scored exactly once, after the threshold is chosen,
  via `offdevice.model.score`.
