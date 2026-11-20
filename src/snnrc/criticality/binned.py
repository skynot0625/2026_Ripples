"""Literature-style temporally binned avalanche definition (primary definition).

Beggs & Plenz (2003) style, applied to the excitatory population only:

1. Analysis interval = steps ``[start_step, end_step)`` (after burn-in).
2. Merge all E spike times in the interval into one population event train
   (simultaneous spikes are separate events, contributing zero intervals).
3. ``delta_t_IEI = mean(t[k+1] - t[k]) = (t_last - t_first) / (n_events - 1)``.
4. Bin width in steps for a multiplier ``m``:
   ``bin_steps = max(1, floor(m * delta_t_IEI / dt + 0.5))`` (round half up).
   The actual width ``bin_steps * dt`` is saved.
5. Bins are aligned to ``start_step``; a trailing partial bin is dropped.
6. Avalanche = maximal run of consecutive non-empty E bins that is preceded
   AND followed by at least one empty bin inside the interval.  Runs touching
   the first or last bin are excluded (counted, never silently dropped).
7. Size = total E spikes in the run; duration = number of bins (and
   ``bins * bin_width_ms``).

No simulator state (pending delay buffer) is used.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np


def mean_population_iei_steps(e_steps: np.ndarray) -> float:
    """Mean inter-event interval (in steps) of the merged E event train."""

    e = np.sort(np.asarray(e_steps, dtype=np.int64))
    if len(e) < 2:
        return float("nan")
    return float((e[-1] - e[0]) / (len(e) - 1))


def bin_steps_for(multiplier: float, iei_steps: float) -> int:
    if not np.isfinite(iei_steps):
        raise ValueError("mean IEI undefined (fewer than 2 E events)")
    return max(1, int(np.floor(multiplier * iei_steps + 0.5)))


def binned_counts(e_steps: np.ndarray, start_step: int, end_step: int, bin_steps: int) -> np.ndarray:
    n_bins = (end_step - start_step) // bin_steps
    e = np.asarray(e_steps, dtype=np.int64)
    e = e[(e >= start_step) & (e < start_step + n_bins * bin_steps)]
    return np.bincount((e - start_step) // bin_steps, minlength=n_bins).astype(np.int64)


def segment_binned(counts: np.ndarray) -> Dict:
    """Avalanches from binned counts.  Returns table + edge exclusions."""

    c = np.asarray(counts)
    active = c > 0
    if len(c) == 0:
        return {"table": [], "excluded_edge": 0}
    d = np.diff(np.r_[0, active.astype(np.int8), 0])
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1) - 1
    cs = np.r_[0, np.cumsum(c)]
    table: List[Dict] = []
    excluded = 0
    for s, e in zip(starts, ends):
        if s == 0 or e == len(c) - 1:
            excluded += 1
            continue
        seg = c[s:e + 1]
        table.append({"start_bin": int(s), "end_bin": int(e), "duration_bins": int(e - s + 1),
                      "size": int(cs[e + 1] - cs[s]), "peak_per_bin": int(seg.max()),
                      "first_bin_count": int(seg[0]), "second_bin_count": int(seg[1]) if len(seg) > 1 else 0})
    for i, r in enumerate(table):
        r["avalanche_id"] = i
    return {"table": table, "excluded_edge": excluded}


def branching_diagnostics(counts: np.ndarray, table: Optional[List[Dict]] = None) -> Dict:
    """DIAGNOSTIC branching estimates on binned counts (not a criticality criterion).

    * ``sigma_first_bin`` (Beggs & Plenz 2003): mean over avalanches of
      n(second bin) / n(first bin) (0 for single-bin avalanches).
    * ``sigma_bin_ratio``: mean over all active bins inside avalanches of
      n(k+1)/n(k), including the closing empty bin.
    Both are biased under subsampling and coarse binning (Priesemann et al.).
    """

    c = np.asarray(counts)
    if table is None:
        table = segment_binned(c)["table"]
    if not table:
        return {"sigma_first_bin": None, "sigma_bin_ratio": None, "n_avalanches": 0,
                "status": "insufficient_data", "role": "diagnostic_only"}
    fb = np.array([r["second_bin_count"] / r["first_bin_count"] for r in table])
    ratios = []
    for r in table:
        seg = c[r["start_bin"]:r["end_bin"] + 2].astype(float)    # include closing empty bin
        ratios.append(seg[1:] / seg[:-1])
    ratios = np.concatenate(ratios)
    return {"sigma_first_bin": float(fb.mean()), "sigma_bin_ratio": float(ratios.mean()),
            "n_avalanches": len(table), "status": "ok", "role": "diagnostic_only"}


def binned_definition(e_steps: np.ndarray, start_step: int, end_step: int, dt_ms: float,
                      bin_steps: int, name: str, role: str, rule: str) -> Dict:
    counts = binned_counts(e_steps, start_step, end_step, bin_steps)
    seg = segment_binned(counts)
    return {"name": name, "role": role, "rule": rule, "bin_steps": int(bin_steps),
            "bin_width_ms": float(bin_steps * dt_ms), "n_bins": int(len(counts)),
            "active_bin_fraction": float(np.mean(counts > 0)) if len(counts) else None,
            "counts": counts, "table": seg["table"], "excluded_edge": seg["excluded_edge"]}
