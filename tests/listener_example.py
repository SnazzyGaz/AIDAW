"""Example listener plugin for PLAINDAW_LISTENERS (used by the tests)."""
import numpy as np


def features(x, sr):
    return {"peak": float(np.max(np.abs(x))), "sr": sr}
