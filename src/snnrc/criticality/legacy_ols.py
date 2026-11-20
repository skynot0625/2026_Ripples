"""Historical exponent estimate: OLS on the log-log empirical PMF.

Kept ONLY as the comparison column ``gamma_OLS_legacy``.  Reconstructed from the
historical analysis: ``P(s) = counts/total`` over unique sizes,
``x = log10 s``, mask ``x > 0.3`` and ``s < 0.9 * max(s)``, linear least squares,
``gamma = -slope``.  The exact historical mask is not recoverable (reconstructed).
This estimator is biased and must never be used as the primary estimate.
"""

from __future__ import annotations

from typing import Dict

import numpy as np


def gamma_ols_legacy(sizes, lower_log10: float = 0.3, upper_frac: float = 0.9,
                     min_events: int = 20, min_points: int = 6) -> Dict:
    sizes = np.asarray(sizes)
    sizes = sizes[sizes > 0]
    base = {"estimator": "OLS_loglog_PMF_legacy", "lower_log10": lower_log10, "upper_frac": upper_frac,
            "template": "SOC notebook cells 6-11 / NPRC slides (reconstructed mask)", "primary": False}
    if len(sizes) < min_events:
        return {**base, "gamma": None, "r2": None, "n_points": 0, "status": "too_few_events"}
    u, c = np.unique(sizes, return_counts=True)
    p = c / c.sum()
    x, y = np.log10(u), np.log10(p)
    mask = (x > lower_log10) & (x < np.log10(u.max() * upper_frac))
    if mask.sum() < min_points:
        return {**base, "gamma": None, "r2": None, "n_points": int(mask.sum()), "status": "too_few_points"}
    slope, intercept = np.polyfit(x[mask], y[mask], 1)
    pred = slope * x[mask] + intercept
    r2 = 1 - np.sum((y[mask] - pred) ** 2) / np.sum((y[mask] - y[mask].mean()) ** 2)
    return {**base, "gamma": float(-slope), "r2": float(r2), "n_points": int(mask.sum()),
            "fit_range": [int(u[mask].min()), int(u[mask].max())], "status": "ok"}
