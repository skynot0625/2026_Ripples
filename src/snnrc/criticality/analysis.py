"""Offline analysis of a saved avalanche run (no simulation).

For every avalanche definition: sizes of closed, post-burn-in avalanches; the
primary heavy-tail protocol (:func:`powerlaw.fit_avalanche_sizes`, includes
``gamma_OLS_legacy`` as a comparison column only); duration statistics, a
size-duration scaling diagnostic and a branching-ratio diagnostic.  Exclusions
(burn-in, truncated at end of run, safety-cutoff avalanches) are counted and
reported, never silent.  Plots are generated from the saved files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

from ..config import AvalancheRunConfig, load_config
from ..provenance import write_json
from . import powerlaw


def load_run(run_dir: Path) -> Dict:
    d = Path(run_dir)
    ev = dict(np.load(d / "spike_events.npz"))
    pop = dict(np.load(d / "population_activity.npz"))
    cfg = load_config(AvalancheRunConfig, d / "config.yaml")
    safety = pd.read_csv(d / "safety_events.csv") if (d / "safety_events.csv").stat().st_size > 1 else pd.DataFrame()
    if "step" not in safety.columns:
        safety = pd.DataFrame({"step": []})
    return {"config": cfg, "events": ev, "population": pop, "kicks": pd.read_csv(d / "kick_events.csv"),
            "safety": safety}


def branching_ratio(activity: np.ndarray, table: pd.DataFrame) -> Dict:
    """Mean over avalanches of mean_t n(t+1)/n(t) for active steps inside the avalanche
    (Beggs & Plenz style descendant/ancestor ratio).  Diagnostic only."""

    vals = []
    for s0, s1 in zip(table["start_step"], table["end_step"]):
        seg = activity[s0:s1 + 2].astype(float)
        anc, desc = seg[:-1], seg[1:]
        m = anc > 0
        if m.any():
            vals.append(np.mean(desc[m] / anc[m]))
    return {"sigma_mean": float(np.mean(vals)) if vals else None, "n_avalanches": len(vals),
            "definition": "mean over avalanches of mean_t n(t+1)/n(t), boundary population, dt bins"}


def size_duration_scaling(table: pd.DataFrame) -> Dict:
    n_zero = int((table["size"] <= 0).sum())
    table = table[table["size"] > 0]
    if len(table) < 10 or table["duration_steps"].nunique() < 3:
        return {"status": "insufficient_data", "n_zero_size_excluded": n_zero}
    g = table.groupby("duration_steps")["size"].mean()
    x, y = np.log10(g.index.values.astype(float)), np.log10(g.values)
    slope, _ = np.polyfit(x, y, 1)
    return {"status": "ok", "exponent_OLS_diagnostic": float(slope), "n_duration_values": int(len(g)),
            "n_zero_size_excluded": n_zero,
            "note": "OLS on log mean size vs log duration; diagnostic only, not a fitted exponent"}


def replay_definition(run_dir: Path, seg) -> pd.DataFrame:
    """Segment a saved run with an additional definition, from saved counts only (no rerun)."""

    from .segmentation import segment_avalanches
    from .simulate import enrich_avalanches

    d = Path(run_dir)
    run = load_run(d)
    cfg = run["config"]
    dt = cfg.reservoir.dt_ms
    nE, nI = run["population"]["n_E"].astype(np.int64), run["population"]["n_I"].astype(np.int64)
    a = nE if seg.boundary_population == "E" else nE + nI
    sz = nE if seg.size_population == "E" else nE + nI
    forced = run["safety"]["step"].to_numpy().astype(int) if len(run["safety"]) else ()
    tab = segment_avalanches(a, sz, run["population"]["pending"], seg, dt, forced_close_steps=forced)
    kicks = run["kicks"].to_dict("records")
    tab = enrich_avalanches(tab, nE, nI, run["events"], kicks, cfg.steps(cfg.burn_in_ms), dt)
    df = pd.DataFrame(tab)
    df.to_csv(d / f"avalanches_{seg.name}.csv", index=False)
    return df


def _fit_block(values: np.ndarray, kw: Dict) -> Dict:
    return powerlaw.fit_avalanche_sizes(values, **kw)


def fit_validity(fit: Dict, gof_alpha: float = 0.1) -> str:
    """Pre-declared validity label (thresholds are never relaxed to obtain a value).

    insufficient_data      -- no S_min candidate met min_tail / min_distinct
    insufficient_range     -- best fit spans < min_decades or fails tail requirements
    power_law_rejected     -- requirements met but bootstrap GoF p < gof_alpha
    plausible_power_law    -- requirements met and GoF p >= gof_alpha
    """

    if fit.get("status") != "ok":
        return "insufficient_data"
    req = fit.get("data_requirements") or {}
    if not all(req.values()):
        return "insufficient_range"
    p = fit.get("gof_p_value")
    if p is None:
        return "gof_not_computed"
    return "plausible_power_law" if p >= gof_alpha else "power_law_rejected"


def analyze_run(run_dir: Path, n_boot_ci: int = 200, n_boot_gof: int = 200, seed: int = 0, min_tail: int = 50,
                min_distinct: int = 10, max_candidates: int = 40, make_plots: bool = True,
                extra_definitions=()) -> Dict:
    """Fit sizes and durations for every saved definition (+ optional offline replays).

    ``extra_definitions``: SegmentationConfigs replayed from the saved counts and
    labelled ``role='sensitivity'``.  Fit thresholds are never relaxed to obtain a
    value; ``status='insufficient_data'`` is reported instead.
    """

    d = Path(run_dir)
    run = load_run(d)
    cfg = run["config"]
    dt = cfg.reservoir.dt_ms
    nE, nI = run["population"]["n_E"].astype(np.int64), run["population"]["n_I"].astype(np.int64)
    kw = dict(n_boot_ci=n_boot_ci, n_boot_gof=n_boot_gof, seed=seed, min_tail=min_tail,
              min_distinct=min_distinct, max_candidates=max_candidates)
    summary: Dict = {"fit_settings": kw, "definitions": {}}
    defs = [(cfg.primary_segmentation, "primary")] + [(sd, "secondary") for sd in cfg.secondary_segmentations]
    for sd in extra_definitions:
        replay_definition(d, sd)
        defs.append((sd, "sensitivity"))
    for sd, role in defs:
        tab = pd.read_csv(d / f"avalanches_{sd.name}.csv")
        entry: Dict = {"role": role, "definition": {
            "boundary_population": sd.boundary_population, "size_population": sd.size_population,
            "close_silence_ms": sd.close_silence_ms, "close_silence_steps": cfg.steps(sd.close_silence_ms),
            "require_no_pending": sd.require_no_pending}}
        summary["definitions"][sd.name] = entry
        if len(tab) == 0:
            entry["status"] = "no_avalanches"
            continue
        keep = (~tab["in_burn_in"].astype(bool)) & tab["closed_normally"].astype(bool) & (~tab["hit_safety_cutoff"].astype(bool))
        used = tab[keep]
        entry["counts"] = {
            "total": int(len(tab)), "in_burn_in": int(tab["in_burn_in"].sum()),
            "not_closed_normally": int((~tab["closed_normally"].astype(bool)).sum()),
            "safety_cutoff": int(tab["hit_safety_cutoff"].sum()), "used_for_fits": int(keep.sum()),
            "kick_triggered_used": int((used["kick_id"] >= 0).sum()) if len(used) else 0}
        if len(used):
            sizes, durs = used["size"].to_numpy(), used["duration_steps"].to_numpy()
            entry["size_summary"] = {"max": int(sizes.max()), "mean": float(sizes.mean()), "median": float(np.median(sizes)),
                                     "n_distinct": int(len(np.unique(sizes)))}
            entry["duration_summary"] = {"max_steps": int(durs.max()), "max_ms": float(durs.max() * dt),
                                         "mean_ms": float(durs.mean() * dt), "median_ms": float(np.median(durs) * dt),
                                         "n_distinct": int(len(np.unique(durs)))}
            u, c = np.unique(sizes, return_counts=True)
            pd.DataFrame({"size": u, "count": c, "pmf": c / c.sum()}).to_csv(d / f"distribution_size_{sd.name}.csv", index=False)
            u, c = np.unique(durs, return_counts=True)
            pd.DataFrame({"duration_steps": u, "duration_ms": u * dt, "count": c, "pmf": c / c.sum()}).to_csv(
                d / f"distribution_duration_{sd.name}.csv", index=False)
        else:
            sizes = durs = np.zeros(0, int)
        fit = _fit_block(sizes, kw)
        fit["definition"] = {"name": sd.name, **entry["definition"]}
        write_json(fit, d / f"fit_size_{sd.name}.json")
        dfit = _fit_block(durs, kw)
        dfit["quantity"] = "avalanche duration in steps (exponent = duration exponent alpha); dt_ms=%g" % dt
        write_json(dfit, d / f"fit_duration_{sd.name}.json")
        mc = fit.get("model_comparison", {})
        entry["size_fit"] = {
            "status": fit["status"], "validity": fit_validity(fit), "gamma_MLE": fit.get("gamma_MLE"), "gamma_ci": [fit.get("gamma_ci_low"), fit.get("gamma_ci_high")],
            "S_min": fit.get("S_min"), "n_tail": fit.get("n_tail"), "n_distinct_tail": fit.get("n_distinct_tail"),
            "ks": fit.get("ks_statistic"), "gof_p": fit.get("gof_p_value"), "fitted_range": fit.get("fitted_range"),
            "decades": fit.get("fitted_range_decades"), "pure_pl_loglik": fit.get("log_likelihood"),
            "truncated": fit.get("truncated_power_law"), "data_requirements": fit.get("data_requirements"),
            "pl_vs_truncated": mc.get("power_law_vs_truncated"), "pl_vs_exponential": mc.get("power_law_vs_exponential"),
            "pl_vs_lognormal": mc.get("power_law_vs_lognormal"), "truncated_vs_lognormal": mc.get("truncated_vs_lognormal"),
            "gamma_OLS_legacy": fit["gamma_OLS_legacy"]}
        entry["duration_fit"] = {"status": dfit["status"], "validity": fit_validity(dfit), "alpha_MLE": dfit.get("gamma_MLE"),
                                 "alpha_ci": [dfit.get("gamma_ci_low"), dfit.get("gamma_ci_high")],
                                 "D_min_steps": dfit.get("S_min"), "n_tail": dfit.get("n_tail"), "ks": dfit.get("ks_statistic"),
                                 "gof_p": dfit.get("gof_p_value"),
                                 "pl_vs_truncated": dfit.get("model_comparison", {}).get("power_law_vs_truncated")}
        act = nE if sd.boundary_population == "E" else nE + nI
        entry["branching"] = branching_ratio(act, used) if len(used) else None
        entry["size_duration_scaling"] = size_duration_scaling(used) if len(used) else {"status": "insufficient_data"}
        if make_plots:
            from ..plotting import plot_size_distribution
            plot_size_distribution(sizes, fit, d / "plots" / f"size_pmf_ccdf_{sd.name}.png", sd.name)
            plot_size_distribution(durs, dfit, d / "plots" / f"duration_pmf_ccdf_{sd.name}.png", sd.name + " (duration, steps)")
    if make_plots:
        from ..plotting import raster_plots
        raster_plots(d)
    metrics = json.loads((d / "activity_metrics.json").read_text())
    summary["activity"] = metrics["analysis_interval"]
    summary["activity_full_run"] = metrics["full_run"]
    summary["kicks"] = {k: metrics[k] for k in ("n_kick_attempts", "n_kicks_successful")}
    summary["kicks"]["n_kicks_failed"] = summary["kicks"]["n_kick_attempts"] - summary["kicks"]["n_kicks_successful"]
    summary["n_safety_events"] = metrics["n_safety_events"]
    write_json(summary, d / "analysis_summary.json")
    return summary
