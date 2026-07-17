"""
Scan paths.BINS (top level) for capture .bins -> regenerate data/metadata.csv.

(run_collection.ps1 v2):

- Captures are the FULL 256 KB NS bank (0x40000): 252 KB immutable app +
  4 KB NV append-log that nv_logger.c rewrites every boot. The CNN watches
  the WHOLE bank (params.REGION_BYTES = 0x40000, MARS-faithful), so dedup
  hashes the full file: only byte-identical captures collapse. Resets of one
  build whose NV tails differ stay as SEPARATE samples -- that NV variation
  is inside the model's input space now, and it is legitimate benign
  variance. Same-build resets share an opt level, so they always land on the
  same side of the train/test split (no leakage).

- Filenames: [O<lvl>_]p<N>_<tag>_rNNN.bin (collection) or legacy benign_*/
  anomaly_* hand-runs. Labels: p0/benign -> 0, everything else -> 1.

- Split is BY BUILD, not by byte-identical twin: every image whose opt level
  is in TEST_OPTS goes to test, the rest to train. The model is evaluated on
  builds it never saw during training, so accuracy/F1 mean something --
  small dataset, but a real generalization test, unlike v1 where test rows
  were byte-identical copies of train rows. Legacy no-opt captures -> train.

Run after a collection:
    python -m offdevice.cnn_quant.data.make_metadata
"""
import csv
import hashlib
import re

from collections import defaultdict
from pathlib import Path
from offdevice.cnn_quant.paths import CSV, BINS
from offdevice.cnn_quant.features import params

CNN_BYTES = params.REGION_BYTES   # dedup key = exactly what the model sees
TEST_OPTS = {"O3"}         # builds held out of training entirely

NAME_RE = re.compile(r"^(?:(O[0-9a-z]+)_)?p(\d+)_")


def parse_name(name: str):
    """filename -> (opt, label) or (None, None) if not a capture."""
    m = NAME_RE.match(name)
    if m:
        opt = m.group(1) if m.group(1) else "legacy"
        return opt, (0 if int(m.group(2)) == 0 else 1)
    if name.startswith("benign"):
        return "legacy", 0
    if name.startswith("anomaly"):
        return "legacy", 1
    return None, None


if __name__ == "__main__":
    bins = sorted(Path(BINS).glob("*.bin"))
    if not bins:
        raise SystemExit(f"no .bin files in {BINS} -- is paths.BINS repointed?")

    groups = defaultdict(list)   # md5(first CNN_BYTES) -> [(name, opt, label)]
    for b in bins:
        opt, label = parse_name(b.name)
        if label is None:
            print(f"  skip {b.name} (unrecognized name, no label)")
            continue
        data = b.read_bytes()
        h = hashlib.md5(data[:CNN_BYTES]).hexdigest()
        groups[h].append((b.name, opt, label))

    rows = []
    stats = defaultdict(int)
    for h, members in sorted(groups.items(), key=lambda kv: kv[1][0][0]):
        members.sort()
        labels = {label for _, _, label in members}
        if len(labels) > 1:
            raise SystemExit(f"hash {h[:12]} appears under BOTH labels: "
                             f"{[n for n, _, _ in members]} -- fix filenames first.")
        opts = {opt for _, opt, _ in members}
        if len(opts) > 1:
            # two opt levels produced the same image: the collection gate should
            # have caught this; keep it in train and say so
            print(f"  WARN: {h[:12]} spans opts {sorted(opts)} -- keeping in train")
        name, opt, label = members[0]
        split = "test" if (len(opts) == 1 and opt in TEST_OPTS) else "train"
        rows.append((name, label, split))
        stats[(split, label)] += 1
        n_resets = len(members)
        print(f"  {name:44s} label={label} {split:5s} ({n_resets} capture(s) collapse here)")

    n_train = stats[("train", 0)] + stats[("train", 1)]
    n_test = stats[("test", 0)] + stats[("test", 1)]
    print(f"\n  train: {n_train} images ({stats[('train',0)]} benign / {stats[('train',1)]} anomaly)")
    print(f"  test : {n_test} images ({stats[('test',0)]} benign / {stats[('test',1)]} anomaly)")
    if n_test == 0:
        print("  NOTE: no test images -- did the collection include the "
              f"{sorted(TEST_OPTS)} opt level(s)?")
    if stats[("train", 0)] == 0 or stats[("train", 1)] == 0:
        print("  WARNING: a class is missing from train -- the model cannot learn.")

    with open(CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["File_Name", "Class_ID", "Type"])
        w.writerows(rows)

    print(f"\nwrote {CSV}: {len(groups)} unique images -> {n_train} train / {n_test} test")
    print("split is by held-out BUILD (opt level), so test accuracy is a real "
          "generalization number, not a fidelity twin.")