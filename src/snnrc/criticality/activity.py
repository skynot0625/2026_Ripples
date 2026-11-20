"""E/I activity metrics.  The structural ratio N_E/N_I is NOT an activity ratio.

    R_total      = total_E_spikes / total_I_spikes
    R_per_neuron = (total_E_spikes / N_E) / (total_I_spikes / N_I) = R_total * N_I / N_E
    E_rate_hz    = total_E_spikes / (N_E * T_seconds)          (same for I)
    E_population_spikes_per_step = total_E_spikes / n_steps    (population-summed spikes per step)

If ``total_I_spikes == 0`` the ratios are ``None`` with ``ratio_status`` set;
no epsilon is ever added.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np


def activity_metrics(n_E: np.ndarray, n_I: np.ndarray, N_E: int, N_I: int, dt_ms: float,
                     per_neuron_counts: Optional[np.ndarray] = None) -> Dict:
    n_E = np.asarray(n_E, dtype=np.int64); n_I = np.asarray(n_I, dtype=np.int64)
    T = len(n_E)
    secs = T * dt_ms / 1000.0
    E, I = int(n_E.sum()), int(n_I.sum())
    out = {
        "n_steps": T, "duration_ms": T * dt_ms,
        "structural_N_E": N_E, "structural_N_I": N_I, "structural_N_E_over_N_I": N_E / N_I if N_I else None,
        "E_total_spikes": E, "I_total_spikes": I,
        "E_spikes_per_neuron": E / N_E, "I_spikes_per_neuron": I / N_I if N_I else None,
        "E_rate_hz": E / N_E / secs if secs else None,
        "I_rate_hz": I / N_I / secs if (secs and N_I) else None,
        "E_population_spikes_per_step": E / T if T else None,
        "I_population_spikes_per_step": I / T if T else None,
        "population_silent_fraction": float(np.mean((n_E + n_I) == 0)) if T else None,
        "population_active_step_fraction": float(np.mean((n_E + n_I) > 0)) if T else None,
        "E_active_step_fraction": float(np.mean(n_E > 0)) if T else None,
    }
    if I > 0:
        out["R_total"] = E / I
        out["R_per_neuron"] = (E / N_E) / (I / N_I)
        out["ratio_status"] = "ok"
    else:
        out["R_total"] = None
        out["R_per_neuron"] = None
        out["ratio_status"] = "undefined_zero_I_spikes"
    if per_neuron_counts is not None:
        c = np.asarray(per_neuron_counts)
        out["E_active_fraction"] = float(np.mean(c[:N_E] > 0))
        out["I_active_fraction"] = float(np.mean(c[N_E:] > 0)) if N_I else None
    return out
