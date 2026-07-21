# Anomaly evaluation — results

This is the committed record of how the shipped model performs against the
collaborator's tampered flash dumps. The model is the Mahalanobis detector in
`offdevice/model/artifacts/mahalanobis.npz` — fitted on the 194-capture benign
dataset (155 train / 39 held-out exam; the tail1 burst collection is excluded,
see `offdevice/DATASET.md`) — with its alarm threshold of 13.909, the 1%
false-positive target. The tampered dumps live in `offdevice/data/anomalies/`,
and they are evaluation-only: nothing here trained the model, and the threshold
was chosen from benign data before any anomaly was scored.

To reproduce, run these two commands from the repo root:

    python -m offdevice.eval.intake
    python -m offdevice.eval.score_anomalies

The first command verifies the delivery (every file must be built on one of the
eight mailed base captures, with changes only inside the 4 KB NV region) and
writes `anomalies_manifest.jsonl`. The second scores everything and writes
`offdevice/eval/results/` (the numbers as JSON, plus three figures). The benign
side of every comparison is the 155 leave-one-out distances stored in the model
artifact — each training capture scored by a model fitted without it. Every
tampered file is also shown next to its own base capture's benign score, so a
detection always means "the tamper moved this file," never "this file was
already suspicious."

## The result in one paragraph

The detector catches every foreign payload of 512 bytes or larger: 8 of 8, and
every catch is genuine — no base capture scores above the alarm line untampered
(the fullest-possible ring, the hardest benign state, sits at 11.706 against
the 13.909 line). Seven of the eight blobs score 19.8 up to 140.3; the eighth —
a 512-byte blob planted on that fullest ring — scores 14.284, clearing the line
by 0.375. That case is the measured detection floor and is reported as such,
not averaged away. The detector misses every tamper of 44 bytes or smaller,
which is what the feature design predicts: the features average over the whole
4096-byte window, so a change of a few bytes is below their hearing. That trade
is the right one for the threat model. A payload that persists malware needs
room, while a flipped timestamp or an out-of-limit temperature value can be
caught by a few lines of firmware logic — no machine learning is needed for
checks that can be written as simple rules. The unresolved question is where
between 8 and 512 bytes the detection floor sits; a follow-up delivery of
in-between sizes is requested.

## Detection at the shipped threshold, by type

| Type | Size changed | Caught | Scores (base → tampered) |
|---|---|---|---|
| Foreign blob, 1024 B | 1024 B | 4 of 4 | 6.2→105.4, 6.3→26.8, 8.2→99.6, 7.7→140.3 |
| Foreign blob, 512 B | 512 B | 4 of 4 | 6.4→19.8, 8.1→69.7, 11.7→14.3, 9.4→20.6 |
| Foreign blob, 8 B | 8 B | 0 of 1 | 6.2→7.6 |
| Stride break | 44 B / 8 B | 0 of 2 | 6.4→6.6, 9.4→9.1 |
| Correlation break | 13 B | 0 of 1 | 11.7→11.7 (moved slightly *down*) |
| Out-of-range value | 2 B | 0 of 1 | 8.2→8.3 |
| Non-monotonic timestamp | 1 B | 0 of 1 | 7.7→7.7 |

The correlation-break file is a plain miss: its tamper moved the score from
11.706 to 11.702. There is no evidence the features see a correlation break —
stated directly rather than dressed up, because that class was never expected
above the floor at this size (13 bytes).

Pooled over all 14 files above, the catch rate is 8 of 14 (57%) at a benign
false-alarm rate of 0.6% (1 of the 155 leave-one-out scores sits above the
threshold, consistent with the 1% design target at the resolution 155 samples
allow). Quote the pooled number only with its composition: payloads of 512
bytes and up were caught 8 of 8, and micro-tampers of 44 bytes and down showed
no detection (0 of 6).

## Floor measurement: journal tampers

Journal tampers are reported here, next to the designed misses, and are not a
headline type. Both were missed, as the floor prediction expects. One detail is
worth keeping: an 8-byte illegal write in the settings-journal area moved its
score by +5.1 (6.3 to 11.4), while 8 bytes hidden among records never moved a
score by more than +1.4. The journal area is far more predictable than record
data, so a tamper there is much louder per byte. The detection floor is
location-dependent.

| File | Size changed | Score (base → tampered) |
|---|---|---|
| Journal tamper (chain gap, J2 with J1 blank) | 8 B | 6.3 → 11.4 |
| Journal tamper (bytes inside a live entry) | 2 B | 8.1 → 8.2 |

## Designed misses

These classes are ones the detector is not supposed to catch, per the
designed-miss record in `offdevice/DATASET.md` (whole-region erase, small
in-range value tweaks, the perfect settings mimic, the defaults downgrade, and
the whole-flash wipe, which belongs to the static hash). None were present in
the current delivery, so the expectations stand untested here and the rows stay
empty. They are on the follow-up request so each expectation can be confirmed
by measurement.

## Live corroboration (the benign side, on hardware)

The false-alarm side of this table is not only the leave-one-out estimate: the
same model ran a 120-minute on-board soak (292 scans, four full ring rotations,
one unbroken run with zero reboots) and produced zero alarms, with a live
benign ceiling of 12.742. A live settings-unit exercise (all four display-unit
states, toggled through a near-full page and a rotation) also stayed benign,
peaking at 12.364. The operating corridor is therefore measured on both sides:
live benign ceiling ~12.7, alarm line 13.909, weakest caught anomaly 14.284.

## The threshold-independent view (appendix)

The ROC curve (`results/roc_appendix.png`) shows the detection-versus-false-alarm
trade-off as the threshold slides, and its area-under-curve summary is 0.824
(0.5 is a coin flip, 1.0 is perfect separation). The shape matters more than
the single number: the curve rises steeply to the shipped operating point (57%
caught at 0.6% false alarms), then goes flat. The flat shelf is the
micro-tampers — no threshold reaches them without an unacceptable false-alarm
rate, because their scores sit inside the benign distribution. The pooled AUC
is pulled down by that deliberately hard half of the delivery; payloads alone
separate almost perfectly.

## Follow-up requested from the collaborator

1. Foreign blobs between the tested sizes — 64, 128, and 256 bytes (and 32 if
   convenient) — to locate the detection floor. This is the highest-value item.
2. The designed-miss classes listed above, so each stated expectation becomes a
   measured row.
3. One or two more journal tampers, so the floor bucket rests on more than two
   files.

Delivery format stays as before: whole 256 KB dumps built on the eight mailed
bases, changes confined to the NV region, filenames carrying the base and the
type. The intake step derives the manifest, so no paperwork is needed.
