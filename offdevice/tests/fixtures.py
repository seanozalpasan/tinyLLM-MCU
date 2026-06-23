"""Deterministic synthetic inputs for tests -- NO external/real data.

A golden-vector regression test only needs a fixed, reproducible input. We
synthesize 256 KB of bytes from a seeded PRNG so the fixture is self-contained
and carries no data-provenance baggage (it does NOT depend on anyone's old
dumps). This is NOT training data; it only pins feature-extraction behavior
against regressions.

The seed and size are FROZEN: changing either invalidates the golden vector.
"""

import numpy as np

# 256 KB, matching the size of a full STM32L562 SRAM snapshot. The exact bytes
# are arbitrary but fixed by the seed.
SYNTHETIC_SEED = 20260616
SYNTHETIC_NBYTES = 262144


def synthetic_dump(seed: int = SYNTHETIC_SEED,
                   nbytes: int = SYNTHETIC_NBYTES) -> bytes:
    """Return a deterministic byte string used as a known feature-test input.

    Uses numpy's PCG64 default_rng, whose stream is stable across numpy
    versions, so the same (seed, nbytes) yields identical bytes everywhere.
    """
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=nbytes, dtype=np.uint8).tobytes()
