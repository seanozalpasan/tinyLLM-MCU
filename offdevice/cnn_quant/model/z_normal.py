"""
Per-feature z-scoring for the CNN-quant pipeline: (x - mu) / sd.
Stats are fit on the TRAIN set only. mu/sd must be saved -- the on-chip C
frontend applies the same transform before inference, or the model infers on a
different distribution than it trained on.
"""
import numpy as np

def fit_normalizer(x_train):
    """Per-feature mean/std from the training set. Guards constant features
    (std ~ 0) so we never divide by zero."""
    mu = x_train.mean(axis=0)
    sd = x_train.std(axis=0)
    sd = np.where(sd < 1e-6, 1.0, sd)          # constant feature 
    return mu, sd

def identity_stats(x_train):
    """mu=0, sd=1 -- makes apply_normalizer a no-op.

    Lets a mode train on raw features (MARS did) while every downstream consumer
    still finds the norm_stats.npz it unconditionally loads.
    """
    shape = x_train.shape[1:]
    return np.zeros(shape, np.float32), np.ones(shape, np.float32)

def apply_normalizer(x, mu, sd):
    """Z-score x with precomputed stats. Shape preserved."""
    return ((x - mu) / sd).astype(np.float32)