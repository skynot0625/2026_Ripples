"""Canonical live features: physical metres, linear powers, or labelled levels.

No per-frame min/max or automatic gain: statistics remain comparable over days.
Missing measurements are absent keys, never imputed zero distances.
"""
from numbers import Real
import math

LIMITS = {
    "hyeol": {**{f"depth_{i}": (0, 4, False) for i in range(9)},
              **{f"coverage_{i}": (0, 1, True) for i in range(9)},
              "ceiling_0": (0, 60, False)},
    "maek": {**{f"mic_level_{i}": (0, 1, True) for i in range(3)},
             **{f"power_{i}": (0, 1, True) for i in range(3)},
             **{f"centroid_{i}": (0, 22050, True) for i in range(3)},
             **{f"band_{band}": (0, 1, True) for band in ("low", "mid", "high")}},
}


def normalize_features(role, features):
    if role not in LIMITS or not isinstance(features, dict) or not features:
        raise ValueError("invalid source/empty sensor features")
    result = {}
    for key, value in features.items():
        if key not in LIMITS[role] or isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"invalid {role} feature: {key}")
        low, high, zero_allowed = LIMITS[role][key]
        value = float(value)
        if (not math.isfinite(value) or not low <= value <= high
                or (not zero_allowed and value == 0) or (key == "ceiling_0" and value == 60)):
            raise ValueError(f"out-of-range {role} feature: {key}")
        result[key] = value
    for i in range(9):
        if f"depth_{i}" in result and result.get(f"coverage_{i}") == 0:
            raise ValueError("depth cannot have zero coverage")
    return result
