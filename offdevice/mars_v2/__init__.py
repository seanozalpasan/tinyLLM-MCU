"""
mars_v2 -- the MARS CNN detector for the 4 KB NV region.

What lives here:
    grid.py       turns the NV region into the (244, 5) record grid
    features.py   the 30 structural spec-fact features
    model.py      the two-branch CNN architecture (grid + structural)
    splits.py     capture listing + stratified fit/calib splits
    anomalies.py  synthetic tampering generator (training data)
    faults.py     benign operational faults (train as benign)
    sandbox.py    CLI that generates a training dataset from clean captures
    score.py      CLI that scores capture files with the shipped model
    weights/      the trained model + its calibrated threshold (mars_v2.json)

"""
from .grid import nv_grid_v2, GRID_SHAPE
from .features import nv_struct_features, FEATURE_NAMES, N_STRUCT
from .model import build_mars_v2
