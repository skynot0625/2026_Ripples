"""Pre-registered sweep manifests: loading, hashing, condition enumeration, config building."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import yaml

from ..config import AvalancheRunConfig, KickConfig, SafetyConfig, ScalingConfig

REQUIRED = ("status", "sweep_id", "reservoir", "grid", "seeds", "run", "fitting", "critical_like_candidate_rule")
FAMILIES = ("B0", "B1")


def load_manifest(path: Path) -> Dict:
    m = yaml.safe_load(Path(path).read_text())
    missing = [k for k in REQUIRED if k not in m]
    if missing:
        raise KeyError(f"manifest missing keys: {missing}")
    fams = set(m["reservoir"]["families"])
    if not fams <= set(FAMILIES):
        raise ValueError(f"unknown families {fams}")
    return m


def manifest_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def control_param(m: Dict) -> str:
    """'g' for historical global-gain sweeps, 'JE' for true excitatory sweeps (J_I fixed)."""

    mode = m["reservoir"]["scaling"]["mode"]
    if mode == "global":
        return "g"
    if mode == "excitatory" and m["reservoir"]["scaling"].get("parameter") == "J_E_exc_x_J_I":
        return "JE_JI"                          # two-dimensional J_E_exc x J_I grid
    if mode == "excitatory":
        if m["reservoir"]["scaling"].get("parameter") != "J_E_exc" or "J_I" not in m["reservoir"]["scaling"]:
            raise ValueError("excitatory sweeps require scaling.parameter = J_E_exc and a fixed scaling.J_I")
        return "JE"
    raise ValueError(f"unknown scaling mode {mode}")


def grid_values_2d(m: Dict) -> tuple:
    dec = int(m["grid"].get("decimals", 1))
    je = sorted(round(float(v), dec) for v in m["grid"]["J_E_exc"])
    ji = sorted(round(float(v), dec) for v in m["grid"]["J_I"])
    if len(set(je)) != len(je) or len(set(ji)) != len(ji):
        raise ValueError("duplicate 2D grid values")
    return je, ji


def grid_values(m: Dict) -> List[float]:
    dec = int(m["grid"].get("decimals", 1))
    if "J_E_exc" in m["grid"]:                     # 2D manifest: the first control dimension
        return grid_values_2d(m)[0]
    if "values" in m["grid"]:                      # explicit list of values
        vals = [round(float(v), dec) for v in m["grid"]["values"]]
        if len(set(vals)) != len(vals):
            raise ValueError("duplicate grid values")
        return sorted(vals)
    vals = set()
    for part in ("coarse", "fine"):
        g = m["grid"][part]
        n = int(round((g["stop"] - g["start"]) / g["step"])) + 1
        vals |= {round(g["start"] + i * g["step"], dec) for i in range(n)}
    return sorted(vals)


@dataclass(frozen=True, order=True)
class Condition:
    """One sweep condition.  ``g_global`` holds the CONTROL VALUE: g_global when
    ``param == 'g'`` (global gain), J_E_exc when ``param == 'JE'`` or ``'JE_JI'`` (excitatory scaling)."""

    family: str
    g_global: float
    seed: int
    param: str = "g"
    J_I: Optional[float] = None            # second control value for 2D grids (param == 'JE_JI')

    @property
    def control_value(self) -> float:
        return self.g_global

    @property
    def condition_id(self) -> str:
        if self.param == "JE_JI":
            return f"{self.family}_JE{self.g_global:05.2f}_JI{self.J_I:05.2f}_s{self.seed}"
        tag = "g" if self.param == "g" else "JE"
        return f"{self.family}_{tag}{self.g_global:05.2f}_s{self.seed}"


def enumerate_conditions(m: Dict, families: Optional[Sequence[str]] = None, g_values: Optional[Sequence[float]] = None,
                         seeds: Optional[Sequence[int]] = None,
                         J_I_values: Optional[Sequence[float]] = None) -> List[Condition]:
    """Deterministic, duplicate-free canonical order: family, g, seed (subsets must be in the manifest)."""

    dec = int(m["grid"].get("decimals", 1))
    if control_param(m) == "JE_JI":
        je, ji = grid_values_2d(m)
        fams = list(m["reservoir"]["families"]) if families is None else list(families)
        ss = list(m["seeds"]["master_seeds"]) if seeds is None else [int(x) for x in seeds]
        gs = je if g_values is None else [round(float(g), dec) for g in g_values]
        jis = ji if J_I_values is None else [round(float(x), dec) for x in J_I_values]
        for g in gs:
            if g not in je:
                raise ValueError(f"J_E_exc {g} not in manifest grid")
        for x in jis:
            if x not in ji:
                raise ValueError(f"J_I {x} not in manifest grid")
        for x in ss:
            if x not in m["seeds"]["master_seeds"]:
                raise ValueError(f"seed {x} not in manifest")
        return sorted({Condition(f, g, s, "JE_JI", j) for f in fams for g in gs for j in jis for s in ss})
    all_g = grid_values(m)
    fams = list(m["reservoir"]["families"]) if families is None else list(families)
    gs = all_g if g_values is None else [round(float(g), dec) for g in g_values]
    ss = list(m["seeds"]["master_seeds"]) if seeds is None else [int(s) for s in seeds]
    for f in fams:
        if f not in m["reservoir"]["families"]:
            raise ValueError(f"family {f} not in manifest")
    for g in gs:
        if g not in all_g:
            raise ValueError(f"control value {g} not in manifest grid")
    for s in ss:
        if s not in m["seeds"]["master_seeds"]:
            raise ValueError(f"seed {s} not in manifest")
    p = control_param(m)
    conds = sorted({Condition(f, g, s, p) for f in fams for g in gs for s in ss})
    return conds


def execution_order(conds: List[Condition], order_seed: int) -> List[Condition]:
    """Fixed pseudo-random execution order (results never depend on it)."""

    perm = np.random.default_rng(int(order_seed)).permutation(len(conds))
    return [conds[i] for i in perm]


def build_run_config(m: Dict, cond: Condition, duration_ms: Optional[float] = None) -> AvalancheRunConfig:
    from ..presets import avalanche_historical_pilot

    r, run = m["reservoir"], m["run"]
    if r["base_preset"] != "nprc_historical_dt01":
        raise ValueError("only base_preset nprc_historical_dt01 is supported")
    cfg = avalanche_historical_pilot(cond.family, cond.g_global, cond.seed,
                                     float(run["duration_ms"] if duration_ms is None else duration_ms))
    res = cfg.reservoir
    res.N_E, res.N_I, res.dt_ms = int(r["N_E"]), int(r["N_I"]), float(r["dt_ms"])
    res.noise.sigma = float(r["noise"]["sigma"]); res.noise.scaling = r["noise"]["scaling"]; res.noise.mode = r["noise"]["mode"]
    if res.adaptation.name != r["adaptation"] or r["semantics"] != "corrected" or res.legacy.any():
        raise ValueError("manifest/preset mismatch (adaptation or semantics)")
    sc = r["scaling"]
    if sc["mode"] == "global":
        if cond.param != "g":
            raise ValueError("global-gain manifest requires 'g' conditions")
    elif sc["mode"] == "excitatory" and sc.get("parameter") == "J_E_exc_x_J_I":
        if cond.param != "JE_JI" or cond.J_I is None:
            raise ValueError("2D excitatory manifest requires 'JE_JI' conditions with J_I")
        res.scaling = ScalingConfig(mode="excitatory", J_E_exc=float(cond.g_global), J_I=float(cond.J_I))
        res.name = res.name.replace("nprc_hist_dt01_", "nprc_hist_dt01_excscale2d_")
    elif sc["mode"] == "excitatory":
        if cond.param != "JE":
            raise ValueError("excitatory manifest requires 'JE' conditions")
        res.scaling = ScalingConfig(mode="excitatory", J_E_exc=float(cond.g_global), J_I=float(sc["J_I"]))
        res.name = res.name.replace("nprc_hist_dt01_", "nprc_hist_dt01_excscale_")
    else:
        raise ValueError("scaling mode mismatch")
    k = run["kick"]
    cfg.kick = KickConfig(amplitude=float(k["amplitude"]), target_population=k["target"],
                          quiet_population=k["quiet_population"], quiet_before_kick_ms=float(k["quiet_before_kick_ms"]),
                          cooldown_ms=float(k["cooldown_ms"]),
                          require_no_active_avalanche=bool(k["require_no_active_tracker_avalanche"]),
                          require_no_pending_spikes=bool(k["require_no_pending"]),
                          on_refractory_target=k["on_refractory_target"])
    s = run["safety"]
    cfg.safety = SafetyConfig(max_size_spikes=int(s["max_size_E_spikes"]), max_duration_ms=float(s["max_duration_ms"]),
                              action=s["action"])
    cfg.burn_in_ms = float(run["burn_in_ms"])
    cfg.protocol_label = f"{m['sweep_id']}::{cond.condition_id}"
    return cfg.validate()


def fit_kwargs(m: Dict) -> Dict:
    f = m["fitting"]
    return dict(n_boot_ci=int(f["bootstrap_ci"]), n_boot_gof=int(f["bootstrap_gof"]), seed=int(f.get("bootstrap_seed", 0)),
                min_tail=int(f["min_tail"]), min_distinct=int(f["min_distinct"]), max_candidates=int(f["max_candidates"]),
                min_decades=float(f["min_decades"]))
