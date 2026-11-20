"""Per-run summary rows and the pre-registered critical-like candidate rule.

The rule (a manifest's ``critical_like_candidate_rule``) was fixed before any sweep
data existed.  Families are never pooled: every function takes rows of exactly
one family.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

PRIMARY = "E_bin_iei_x1.0"


def run_row(run_dir: Path) -> Dict:
    d = Path(run_dir)
    c = json.loads((d / "COMPLETED.json").read_text())
    act = json.loads((d / "activity_metrics.json").read_text())
    b = json.loads((d / "stage3_5" / "binned_summary.json").read_text())
    t = json.loads((d / "timing.json").read_text())
    p = b["definitions"].get(PRIMARY, {})
    sf, df = p.get("size_fit", {}), p.get("duration_fit", {})
    sd = p.get("size_duration_scaling", {}) or {}
    ai = act["analysis_interval"]
    ci = sf.get("gamma_ci") or [None, None]
    return {
        "condition_id": c["condition_id"], "family": c["family"], "g_global": c["g_global"], "seed": c["seed"],
        "control_parameter": c.get("control_parameter", "g_global"), "control_value": c.get("control_value", c["g_global"]),
        "J_I": c.get("J_I"),
        "mode": c["mode"], "run_status": act["run_status"],
        "completed_duration_ms": act["termination"]["completed_duration_ms"],
        "n_safety_events": act["n_safety_events"], "kicks_successful": act["n_kicks_successful"],
        "E_total_spikes": ai["E_total_spikes"], "I_total_spikes": ai["I_total_spikes"],
        "E_pop_spikes_per_step": ai["E_population_spikes_per_step"], "I_pop_spikes_per_step": ai["I_population_spikes_per_step"],
        "E_rate_hz": ai["E_rate_hz"], "I_rate_hz": ai["I_rate_hz"], "R_total": ai["R_total"], "R_per_neuron": ai["R_per_neuron"],
        "primary_bin_ms": p.get("bin_width_ms"), "active_bin_fraction": p.get("active_bin_fraction"),
        "n_avalanches": p.get("n_avalanches", 0), "max_size": (p.get("size") or {}).get("max"),
        "max_duration_ms": (p.get("duration") or {}).get("max_ms"),
        "size_validity": sf.get("validity"), "gamma_MLE": sf.get("gamma_MLE"),
        "gamma_ci_low": ci[0], "gamma_ci_high": ci[1], "size_n_tail": sf.get("n_tail"), "size_gof_p": sf.get("gof_p"),
        "duration_validity": df.get("validity"), "alpha_MLE": df.get("gamma_MLE"), "duration_n_tail": df.get("n_tail"),
        "size_duration_slope": sd.get("slope_OLS_diagnostic"),
        "branching_sigma_first_bin_primary": (b.get("branching", {}).get("primary_iei_x1.0") or {}).get("sigma_first_bin"),
        "simulation_wall_s": t["simulation_wall_s"], "analysis_wall_s": t["analysis_wall_s"],
        "reservoir_id": c["reservoir_id"], "base_structure_id": c["base_structure_id"],
    }


def collect_rows(out_dir: Path, manifest_sha256: Optional[str] = None) -> pd.DataFrame:
    """Rows of all completed runs.  With ``manifest_sha256`` every run must have been produced
    under exactly that manifest (guards against mixing runs from different experiments)."""

    rows = []
    for d in sorted((Path(out_dir) / "runs").iterdir()):
        if not (d / "COMPLETED.json").exists():
            continue
        if manifest_sha256 is not None:
            got = json.loads((d / "COMPLETED.json").read_text()).get("manifest_sha256")
            if got != manifest_sha256:
                raise ValueError(f"{d.name} was produced under manifest {got}, not {manifest_sha256}")
        rows.append(run_row(d))
    return pd.DataFrame(rows)


def eligible(row: Dict) -> bool:
    abf = row.get("active_bin_fraction")
    return bool(
        row["run_status"] == "completed_normal" and row["n_safety_events"] == 0
        and row["size_validity"] == "plausible_power_law" and row["duration_validity"] == "plausible_power_law"
        and abf is not None and 0.02 <= abf <= 0.98
        and (row["E_total_spikes"] or 0) > 0 and (row["I_total_spikes"] or 0) > 0)


def select_candidate(rows: pd.DataFrame, family: str, min_eligible_seeds: int = 3) -> Dict:
    if rows["family"].nunique() != 1 or rows["family"].iloc[0] != family:
        raise ValueError("candidate selection must receive rows of exactly one family (no pooling)")
    rows = rows.copy()
    rows["eligible"] = [eligible(r) for r in rows.to_dict("records")]
    table: List[Dict] = []
    for g, grp in rows.groupby("g_global"):
        e = grp[grp["eligible"]]
        entry = {"g_global": float(g), "n_seeds": int(len(grp)), "n_eligible": int(len(e)),
                 "stable": bool(len(e) >= min_eligible_seeds)}
        if entry["stable"]:
            gam, alp = e["gamma_MLE"].mean(), e["alpha_MLE"].mean()
            half = ((e["gamma_ci_high"] - e["gamma_ci_low"]) / 2).mean()
            R = e["R_total"].mean()
            slope = e["size_duration_slope"].mean()
            pred = (alp - 1.0) / (gam - 1.0)
            entry.update(mean_gamma=gam, mean_alpha=alp, mean_ci_half=half, mean_R_total=R, mean_slope=slope,
                         crackling_prediction=pred,
                         S=abs(gam - 1.5) / max(half, 0.05) + abs(math.log(R / 4.0)) / math.log(1.25)
                         + abs(slope - pred) / 0.2)
        table.append(entry)
    stable = [t for t in table if t["stable"]]
    if not stable:
        return {"family": family, "result": "no critical-like candidate under the preregistered criteria",
                "candidate_g": None, "table": table}
    best = min(stable, key=lambda t: (t["S"], t["g_global"]))
    g = best["g_global"]
    return {"family": family, "result": "candidate", "candidate_g": g,
            "subcritical_g": round(g - 1.0, 1), "supercritical_g": round(g + 1.0, 1), "table": table}
