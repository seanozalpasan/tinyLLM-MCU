"""
This generates the training data for mars_v2: it takes clean captures and
creates tampered versions of them (every anomaly family, with wide random
magnitudes and placements) plus benign-fault examples (torn tails, fresh page
opens, mid-open resets) that train as BENIGN so real field states don't get
flagged. Everything lands in the package's data/sandbox dir with a manifest
that records the exact parameters behind every file.

    python -m offdevice.mars_v2.sandbox --seed 5000
"""
from __future__ import annotations

import argparse
import json
import random

import numpy as np

from offdevice.nv import spec
from offdevice.nv.parse import slice_nv

from .anomalies import synthesize
from .faults import FAULT_KINDS, make_fault
from .paths import SANDBOX, SBX_MANIFEST
from .splits import trainable_files

# 4242 trained the model that ships in weights/ -- reusing it would make a new
# dataset look comparable to the original when it is not
RESERVED_SEED = 4242


def _samplers(random_gen: random.Random):
    """family -> (count, parameter sampler). Wide random ranges on purpose."""
    def log_uniform(low, high):
        return int(round(np.exp(random_gen.uniform(np.log(low), np.log(high)))))
    return {
        "out_of_range": (180, lambda: {
            "type": "out_of_range", "magnitude": log_uniform(1, 20000),
            "direction": random_gen.choice(("above", "below")),
            "channel": random_gen.choice(("temp", "hum", "press"))}),
        "nonmonotonic_ts": (150, lambda: {
            "type": "nonmonotonic_ts", "magnitude": log_uniform(15, 2000),
            "run": random_gen.randint(1, 6)}),
        "correlation_break": (180, lambda: {
            "type": "correlation_break", "magnitude": random_gen.randint(1, 24),
            "run": random_gen.randint(3, 50), "sign": random_gen.choice((1, -1))}),
        "blob": (200, lambda: {
            "type": "blob", "size": log_uniform(16, 1500),
            "content": random_gen.choice(("random", "code", "text")),
            "placement": random_gen.choice(("records", "journal", "header", "nonpage"))}),
        "stride_break": (100, lambda: {
            "type": "stride_break", "magnitude": random_gen.randint(1, 10)}),
        "journal_tamper": (120, lambda: {
            "type": "journal_tamper",
            "mode": random_gen.choice(("reserved0", "op_backwards", "unit_bad"))}),
        "bitflip": (180, lambda: {
            "type": "bitflip", "magnitude": log_uniform(1, 32),
            "region": random_gen.choice(("records", "header", "journal"))}),
        "header": (160, lambda: {
            "type": "header",
            "mode": random_gen.choice(("full_smash", "version_flip", "stat_rewrite",
                                       "page_seq_swap"))}),
    }


def gen(seed: int) -> int:
    random_gen = random.Random(seed)
    bases = trainable_files()
    print(f"[gen] {len(bases)} sandbox bases (holdout + quarantine excluded), seed {seed}")
    (SANDBOX / "anom").mkdir(parents=True, exist_ok=True)
    (SANDBOX / "fault").mkdir(parents=True, exist_ok=True)
    manifest_rows = []

    for family, (target_count, make_params) in _samplers(random_gen).items():
        made = 0
        tries = 0
        while made < target_count and tries < target_count * 6:
            tries += 1
            base = bases[random_gen.randrange(len(bases))]
            full_dump = base.read_bytes()
            nv_region = slice_nv(full_dump)
            result = synthesize(nv_region, make_params(), random_gen,
                                code_src=full_dump[:spec.STATIC_SIZE])
            if result is None or result[0] == nv_region:
                continue
            tampered_bytes, params_used = result
            file_name = f"sbx__{family}__{made:04d}.bin"
            (SANDBOX / "anom" / file_name).write_bytes(tampered_bytes)
            manifest_rows.append({"file": f"anom/{file_name}", "label": "anomalous",
                                  "family": family, "params": params_used,
                                  "base": base.name})
            made += 1
        print(f"[gen]   {family:18s} {made}/{target_count}")

    faults_made = 0
    for kind in FAULT_KINDS:
        shuffled_bases = bases[:]
        random_gen.shuffle(shuffled_bases)
        made_this_kind = 0
        for base in shuffled_bases:
            if made_this_kind >= 12:
                break
            result = make_fault(slice_nv(base.read_bytes()), kind, random_gen)
            if result is None:
                continue
            file_name = f"sbx__fault_{kind}__{made_this_kind:03d}.bin"
            (SANDBOX / "fault" / file_name).write_bytes(result)
            manifest_rows.append({"file": f"fault/{file_name}",
                                  "label": "benign_fault",
                                  "family": kind, "base": base.name})
            made_this_kind += 1
            faults_made += 1
    print(f"[gen]   benign faults      {faults_made}")

    with open(SBX_MANIFEST, "w", encoding="utf-8") as manifest_file:
        for row in manifest_rows:
            manifest_file.write(json.dumps(row) + "\n")
    print(f"[gen] wrote {len(manifest_rows)} sandbox files + manifest -> {SANDBOX}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True,
                        help=f"dataset seed (NEVER {RESERVED_SEED} -- that one "
                             f"trained the shipped model)")
    args = parser.parse_args()
    if args.seed == RESERVED_SEED:
        raise SystemExit(f"seed {RESERVED_SEED} trained the shipped model -- "
                         f"pick a different one")
    raise SystemExit(gen(args.seed))
