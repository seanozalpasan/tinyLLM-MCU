"""mars_v2 -- development home for the MARS CNN detector (post-exam).

The graded, frozen detector lives in offdevice/exam/og_mars_v2.py and never
changes (frozen.json pins it; results in offdevice/exam/report.md). THIS
package is where development continues: same encoding, same architecture,
free to evolve. Numbers produced here are new experiments, not the exam's.

Self-contained by design: everything exam- or AE-owned is PORTED in (grid
encoding, structural features, splits, generators) -- the only outside imports
are the shared NV ground truth (offdevice.nv spec/parse), which every detector
must agree on. parity_check.py proves the ported encoding still matches the
graded one; re-run it after touching grid.py or features.py.
"""
from .grid import nv_grid_v2, GRID_SHAPE
from .features import nv_struct_features, FEATURE_NAMES, N_STRUCT
from .model import build_mars_v2
