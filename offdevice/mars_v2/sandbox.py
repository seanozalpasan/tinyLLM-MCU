"""

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


def _samplers(rng: random.Random):
    """family -> (count, param sampler). Wide random ranges, not a battery grid."""
    def log_uniform(lo, hi):
        return int(round(np.exp(rng.uniform(np.log(lo), np.log(hi)))))
    return {
        "out_of_range": (180, lambda: {
            "type": "out_of_range", "magnitude": log_uniform(1, 20000),
            "direction": rng.choice(("above", "below")),
            "channel": rng.choice(("temp", "hum", "press"))}),
        "nonmonotonic_ts": (150, lambda: {
            "type": "nonmonotonic_ts", "magnitude": log_uniform(15, 2000),
            "run": rng.randint(1, 6)}),
        "correlation_break": (180, lambda: {
            "type": "correlation_break", "magnitude": rng.randint(1, 24),
            "run": rng.randint(3, 50), "sign": rng.choice((1, -1))}),
        "blob": (200, lambda: {
            "type": "blob", "size": log_uniform(16, 1500),
            "content": rng.choice(("random", "code", "text")),
            "placement": rng.choice(("records", "journal", "header", "nonpage"))}),
        "stride_break": (100, lambda: {
            "type": "stride_break", "magnitude": rng.randint(1, 10)}),
        "journal_tamper": (120, lambda: {
            "type": "journal_tamper",
            "mode": rng.choice(("reserved0", "op_backwards", "unit_bad"))}),
        "bitflip": (180, lambda: {
            "type": "bitflip", "magnitude": log_uniform(1, 32),
            "region": rng.choice(("records", "header", "journal"))}),
        "header": (160, lambda: {
            "type": "header",
            "mode": rng.choice(("full_smash", "version_flip", "stat_rewrite",
                                "page_seq_swap"))}),
    }


def gen(seed: int) -> int:
    rng = random.Random(seed)
    bases = trainable_files()
    print(f"[gen] {len(bases)} sandbox bases (holdout + quarantine excluded), seed {seed}")
    (SANDBOX / "anom").mkdir(parents=True, exist_ok=True)
    (SANDBOX / "fault").mkdir(parents=True, exist_ok=True)
    rows = []
    for family, (count, sample) in _samplers(rng).items():
        made, tries = 0, 0
        while made < count and tries < count * 6:
            tries += 1
            base = bases[rng.randrange(len(bases))]
            raw = base.read_bytes()
            nv = slice_nv(raw)
            out = synthesize(nv, sample(), rng, code_src=raw[:spec.STATIC_SIZE])
            if out is None or out[0] == nv:
                continue
            data, params = out
            name = f"sbx__{family}__{made:04d}.bin"
            (SANDBOX / "anom" / name).write_bytes(data)
            rows.append({"file": f"anom/{name}", "label": "anomalous",
                         "family": family, "params": params, "base": base.name})
            made += 1
        print(f"[gen]   {family:18s} {made}/{count}")
    made = 0
    for kind in FAULT_KINDS:
        order = bases[:]
        rng.shuffle(order)
        k = 0
        for base in order:
            if k >= 12:
                break
            out = make_fault(slice_nv(base.read_bytes()), kind, rng)
            if out is None:
                continue
            name = f"sbx__fault_{kind}__{k:03d}.bin"
            (SANDBOX / "fault" / name).write_bytes(out)
            rows.append({"file": f"fault/{name}", "label": "benign_fault",
                         "family": kind, "base": base.name})
            k += 1
            made += 1
    print(f"[gen]   benign faults      {made}")
    with open(SBX_MANIFEST, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"[gen] wrote {len(rows)} sandbox files + manifest -> {SANDBOX}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True,
                    help="dev seed (NEVER 4242 -- that is the frozen exam sandbox)")
    args = ap.parse_args()
    if args.seed == 4242:
        raise SystemExit("seed 4242 is the frozen exam sandbox seed -- pick another")
    raise SystemExit(gen(args.seed))
