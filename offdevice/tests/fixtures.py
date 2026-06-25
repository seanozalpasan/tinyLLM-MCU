"""Deterministic synthetic input for tests -- no external/real data.

256 KB of bytes from a seeded PRNG: a self-contained, reproducible input for the
golden-vector regression. NOT training data. Seed and size are FROZEN -- changing
either invalidates the golden vector.
"""

import numpy as np

# 256 KB, matching the size of a full NS-flash dump (0x08040000-0x0807FFFF). The exact bytes
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
