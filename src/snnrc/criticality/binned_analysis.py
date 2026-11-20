"""Offline analysis: E-only binned avalanche definitions (no rerun).

Definition roles (fixed before looking at any gamma):

* ``primary_scientific``      -- E-only, bin = 1.0 x mean E-population IEI
* ``bin_width_sensitivity``   -- 0.5 x and 2.0 x mean IEI
* ``historical_fixed_bin``    -- fixed 0.1, 1.0, 5.0 ms bins (diagnostic)
* ``historical_mechanistic``  -- the online tracker "5 ms E-silence + no pending"
                                 definition (diagnostic; uses hidden simulator state)

All are computed from the saved ``spike_events.npz`` of the analysis interval
(after burn-in).  Fit thresholds come from ``fit_kw`` and are never relaxed;
validity labels come from :func:`analysis.fit_validity`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import ms_to_steps
from ..provenance import write_json
from . import powerlaw
from .analysis import fit_validity, load_run
from .binned import bin_steps_for, binned_counts, binned_definition, branching_diagnostics, mean_population_iei_steps

IEI_MULTIPLIERS = {"iei_x1.0": (1.0, "primary_scientific"), "iei_x0.5": (0.5, "bin_width_sensitivity"),
                   "iei_x2.0": (2.0, "bin_width_sensitivity")}
FIXED_BINS_MS = (0.1, 1.0, 5.0)
BURST_THRESHOLD_E_SPIKES = 100


def _compact_fit(fit: Dict) -> Dict:
    mc = fit.get("model_comparison", {})
    keep = lambda d, ks: None if d is None else {k: d.get(k) for k in ks}  # noqa: E731
    cmp_keys = ("R", "normalized_R", "LR_statistic", "p_value", "preferred")
    return {
        "status": fit["status"], "validity": fit_validity(fit), "gamma_MLE": fit.get("gamma_MLE"),
        "gamma_ci": [fit.get("gamma_ci_low"), fit.get("gamma_ci_high")], "S_min": fit.get("S_min"),
        "n_tail": fit.get("n_tail"), "n_distinct_tail": fit.get("n_distinct_tail"),
        "fitted_range": fit.get("fitted_range"), "decades": fit.get("fitted_range_decades"),
        "ks": fit.get("ks_statistic"), "gof_p": fit.get("gof_p_value"), "pure_pl_loglik": fit.get("log_likelihood"),
        "truncated": keep(fit.get("truncated_power_law"), ("gamma", "s_c", "loglik")),
        "pl_vs_truncated": keep(mc.get("power_law_vs_truncated"), cmp_keys),
        "pl_vs_exponential": keep(mc.get("power_law_vs_exponential"), cmp_keys),
        "pl_vs_lognormal": keep(mc.get("power_law_vs_lognormal"), cmp_keys),
        "truncated_vs_lognormal": keep(mc.get("truncated_vs_lognormal"), cmp_keys),
        "gamma_OLS_legacy": {k: fit["gamma_OLS_legacy"].get(k) for k in ("gamma", "r2", "n_points", "status")},
        "data_requirements": fit.get("data_requirements"),
    }


def _size_duration(sizes: np.ndarray, dur: np.ndarray) -> Dict:
    ok = sizes > 0
    sizes, dur = sizes[ok], dur[ok]
    if len(sizes) < 10 or len(np.unique(dur)) < 3:
        return {"status": "insufficient_data"}
    df = pd.DataFrame({"s": sizes, "d": dur}).groupby("d")["s"].mean()
    slope, _ = np.polyfit(np.log10(df.index.values.astype(float)), np.log10(df.values), 1)
    return {"status": "ok", "slope_OLS_diagnostic": float(slope), "n_duration_values": int(len(df)),
            "note": "OLS of log mean size vs log duration (bins); diagnostic only"}


def _analyze_table(name: str, role: str, bin_width_ms: Optional[float], sizes: np.ndarray, dur_bins: np.ndarray,
                   dur_ms: np.ndarray, interval_s: float, fit_kw: Dict, out: Path) -> Dict:
    fit = powerlaw.fit_avalanche_sizes(sizes, **fit_kw)
    dfit = powerlaw.fit_avalanche_sizes(dur_bins, **fit_kw)
    write_json({**fit, "definition": name, "role": role}, out / f"fit_size_{name}.json")
    write_json({**dfit, "definition": name, "role": role, "quantity": "duration in bins/steps"},
               out / f"fit_duration_{name}.json")
    if len(sizes):
        u, c = np.unique(sizes, return_counts=True)
        pd.DataFrame({"size": u, "count": c, "pmf": c / c.sum()}).to_csv(out / f"distribution_size_{name}.csv", index=False)
        u, c = np.unique(dur_bins, return_counts=True)
        pd.DataFrame({"duration_bins": u, "count": c, "pmf": c / c.sum()}).to_csv(
            out / f"distribution_duration_{name}.csv", index=False)
    return {
        "role": role, "bin_width_ms": bin_width_ms, "n_avalanches": int(len(sizes)),
        "avalanches_per_s": float(len(sizes) / interval_s),
        "bursts_ge100E_per_s": float(np.sum(sizes >= BURST_THRESHOLD_E_SPIKES) / interval_s),
        "size": {"max": int(sizes.max()) if len(sizes) else None, "mean": float(sizes.mean()) if len(sizes) else None,
                 "median": float(np.median(sizes)) if len(sizes) else None},
        "duration": {"max_bins": int(dur_bins.max()) if len(dur_bins) else None,
                     "max_ms": float(dur_ms.max()) if len(dur_ms) else None,
                     "mean_ms": float(dur_ms.mean()) if len(dur_ms) else None},
        "size_fit": _compact_fit(fit), "duration_fit": _compact_fit(dfit),
        "size_duration_scaling": _size_duration(sizes, dur_bins),
    }


def analyze_binned_run(run_dir: Path, fit_kw: Dict, make_plots: bool = True) -> Dict:
    d = Path(run_dir)
    run = load_run(d)
    cfg = run["config"]
    dt = cfg.reservoir.dt_ms
    T = len(run["population"]["n_E"])
    start = cfg.steps(cfg.burn_in_ms)
    ev = run["events"]
    e_steps = ev["step"][(ev["population"] == 0) & (ev["step"] >= start)]
    interval_s = (T - start) * dt / 1000.0
    iei = mean_population_iei_steps(e_steps)
    out_dir = d / "stage3_5"
    out_dir.mkdir(exist_ok=True)
    summary: Dict = {
        "run_dir": str(d), "dt_ms": dt, "analysis_start_step": int(start), "analysis_end_step": int(T),
        "n_E_events": int(len(e_steps)),
        "mean_E_population_IEI": {"steps": iei, "ms": iei * dt,
                                  "rule": "(t_last - t_first)/(n_events - 1) over merged E events after burn-in"},
        "bin_rounding_rule": "bin_steps = max(1, floor(m * IEI_steps + 0.5))",
        "definitions": {},
    }
    binned_defs: List[Dict] = []
    iei_ok = bool(np.isfinite(iei)) and T > start
    summary["mean_E_population_IEI"]["defined"] = iei_ok
    for name, (m, role) in IEI_MULTIPLIERS.items():
        if not iei_ok:
            summary["definitions"][f"E_bin_{name}"] = {
                "role": role, "status": "insufficient_data_mean_IEI_undefined", "bin_width_ms": None,
                "n_avalanches": 0, "size_fit": {"status": "insufficient_data", "validity": "insufficient_data"},
                "duration_fit": {"status": "insufficient_data", "validity": "insufficient_data"}}
            continue
        b = bin_steps_for(m, iei)
        binned_defs.append(binned_definition(e_steps, start, T, dt, b, f"E_bin_{name}", role,
                                             f"E-only bins of {m} x mean IEI ({m * iei * dt:.4f} ms requested)"))
    for ms in (FIXED_BINS_MS if T > start else ()):
        b = ms_to_steps(ms, dt, "fixed bin")
        binned_defs.append(binned_definition(e_steps, start, T, dt, b, f"E_bin_fixed_{ms:g}ms",
                                             "historical_fixed_bin", f"E-only fixed {ms:g} ms bins"))
    for bd in binned_defs:
        tab = pd.DataFrame(bd["table"])
        if len(tab):
            tab["start_step"] = start + tab["start_bin"] * bd["bin_steps"]
            tab["duration_ms"] = tab["duration_bins"] * bd["bin_width_ms"]
        tab.to_csv(out_dir / f"avalanches_{bd['name']}.csv", index=False)
        sizes = tab["size"].to_numpy() if len(tab) else np.zeros(0, int)
        dur = tab["duration_bins"].to_numpy() if len(tab) else np.zeros(0, int)
        entry = _analyze_table(bd["name"], bd["role"], bd["bin_width_ms"], sizes, dur, dur * bd["bin_width_ms"],
                               max(interval_s, 1e-9), fit_kw, out_dir)
        entry.update(rule=bd["rule"], bin_steps=bd["bin_steps"], n_bins=bd["n_bins"],
                     active_bin_fraction=bd["active_bin_fraction"], excluded_edge_runs=bd["excluded_edge"],
                     branching_at_this_width=branching_diagnostics(bd["counts"], bd["table"]))
        summary["definitions"][bd["name"]] = entry
    # historical mechanistic tracker definition (primary online tracker), post-burn-in, closed normally
    try:
        old = pd.read_csv(d / f"avalanches_{cfg.primary_segmentation.name}.csv")
    except pd.errors.EmptyDataError:
        old = pd.DataFrame(columns=["size", "duration_steps", "in_burn_in", "closed_normally", "hit_safety_cutoff"])
    if len(old):
        old = old[(~old["in_burn_in"].astype(bool)) & old["closed_normally"].astype(bool) & (~old["hit_safety_cutoff"].astype(bool))]
    e = _analyze_table("tracker_5ms_silence_plus_pending", "historical_mechanistic", None,
                       old["size"].to_numpy(dtype=np.int64), old["duration_steps"].to_numpy(dtype=np.int64),
                       old["duration_steps"].to_numpy(dtype=float) * dt, max(interval_s, 1e-9), fit_kw, out_dir)
    e["rule"] = "Stage 3 primary: E boundary, close after 5 ms E-silence AND empty delay buffer (hidden simulator state)"
    summary["definitions"]["tracker_5ms_silence_plus_pending"] = e

    # branching diagnostics at the three requested widths
    import json

    fp = json.loads((d / "reservoir_fingerprint.json").read_text())
    hist = np.asarray(fp["delay_steps_histogram"])
    mean_delay_steps = float((hist * np.arange(len(hist))).sum() / hist.sum())
    widths = {"fixed_1ms": ms_to_steps(1.0, dt, "1 ms"),
              "mean_recurrent_delay": max(1, int(np.floor(mean_delay_steps + 0.5)))}
    if iei_ok:
        widths = {"primary_iei_x1.0": bin_steps_for(1.0, iei), **widths}
    br = {}
    for k, b in (widths.items() if T > start else ()):
        counts = binned_counts(e_steps, start, T, b)
        br[k] = {"bin_steps": int(b), "bin_width_ms": b * dt, **branching_diagnostics(counts)}
    if "mean_recurrent_delay" in br:
        br["mean_recurrent_delay"]["mean_delay_ms"] = mean_delay_steps * dt
    br["note"] = ("Branching ratios are DIAGNOSTICS only (estimator validated only on a synthetic Galton-Watson "
                  "process with generation-matched bins; biased under subsampling and bin/generation mismatch).")
    summary["branching"] = br

    if make_plots and iei_ok:
        from ..plotting import plot_size_distribution
        name = "E_bin_iei_x1.0"
        tab = pd.read_csv(out_dir / f"avalanches_{name}.csv")
        fit = __import__("json").loads((out_dir / f"fit_size_{name}.json").read_text())
        plot_size_distribution(tab["size"].to_numpy() if len(tab) else np.zeros(0), fit,
                               out_dir / f"size_pmf_ccdf_{name}.png", f"{name} (bin {summary['definitions'][name]['bin_width_ms']:.1f} ms)")
    write_json(summary, out_dir / "binned_summary.json")
    return summary
