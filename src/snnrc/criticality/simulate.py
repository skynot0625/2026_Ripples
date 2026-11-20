"""Kick-driven, task-free avalanche simulation of the biological reservoir.

Per step ``t`` (order matters and is fixed):

1. kick eligibility from information up to ``t-1`` (quiet counter, primary
   tracker state, ring occupancy, cooldown); on eligibility one attempt is made
   and logged; a successful kick adds ``amplitude`` to the target's voltage at
   step ``t`` (after leak, before threshold);
2. one reservoir step (noise from the ``noise`` seed stream);
3. log spike ids, n_E(t), n_I(t), pending(t);
4. update the quiet counter (``quiet_population`` activity at t);
5. update the primary online tracker; if the prospective avalanche would exceed
   the safety limits the avalanche is force-closed at t (``hit_safety_cutoff``)
   and, for ``action='reset_state'``, the reservoir dynamics are reset (logged).

All secondary avalanche definitions are computed afterwards from the saved
raw counts only (no rerun), with the same forced-closure steps.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import torch

from ..config import AvalancheRunConfig, save_config, to_dict
from ..logging import PopulationActivityLogger, SpikeEventLogger, save_table
from ..provenance import environment, write_json
from ..reservoir import BiologicalEIReservoir, KickController
from ..seeding import numpy_rng
from .activity import activity_metrics
from .segmentation import AvalancheTracker, segment_avalanches

KICK_COLUMNS = ["kick_index", "simulation_step", "time_ms", "target_population", "target_neuron_id", "amplitude",
                "application", "success", "failure_reason", "state_before_v", "state_before_w",
                "state_before_ref", "evoked_spike_at_kick_step", "first_spike_latency_steps",
                "avalanche_id_triggered"]


def _definition_arrays(seg, n_E, n_I):
    a = n_E if seg.boundary_population == "E" else n_E + n_I
    s = n_E if seg.size_population == "E" else n_E + n_I
    return a, s


def enrich_avalanches(table: List[Dict], n_E: np.ndarray, n_I: np.ndarray, events: Dict[str, np.ndarray],
                      kicks: List[Dict], burn_in_steps: int, dt_ms: float) -> List[Dict]:
    cE, cI = np.r_[0, np.cumsum(n_E)], np.r_[0, np.cumsum(n_I)]
    ev_step, ev_id, ev_pop = events["step"], events["neuron_id"], events["population"]
    ok_kicks = sorted([k for k in kicks if k["success"]], key=lambda k: k["simulation_step"])
    kick_steps = np.array([k["simulation_step"] for k in ok_kicks], dtype=np.int64)
    all_attempt_steps = np.array(sorted(k["simulation_step"] for k in kicks), dtype=np.int64)
    prev_end = None
    claimed = set()
    for r in table:
        s0, s1 = r["start_step"], r["end_step"]
        r["duration_ms"] = r["duration_steps"] * dt_ms
        r["size_E_spikes"] = int(cE[s1 + 1] - cE[s0])
        r["size_I_spikes"] = int(cI[s1 + 1] - cI[s0])
        r["size_total_spikes"] = r["size_E_spikes"] + r["size_I_spikes"]
        lo, hi = np.searchsorted(ev_step, s0, "left"), np.searchsorted(ev_step, s1, "right")
        ids, pops = ev_id[lo:hi], ev_pop[lo:hi]
        r["unique_E_neurons"] = int(len(np.unique(ids[pops == 0])))
        r["unique_I_neurons"] = int(len(np.unique(ids[pops == 1])))
        r["peak_E_spikes_per_step"] = int(n_E[s0:s1 + 1].max())
        r["peak_I_spikes_per_step"] = int(n_I[s0:s1 + 1].max())
        r["peak_total_spikes_per_step"] = int((n_E + n_I)[s0:s1 + 1].max())
        r["inter_avalanche_interval_steps"] = -1 if prev_end is None else int(s0 - prev_end)
        r["in_burn_in"] = bool(s0 < burn_in_steps)
        # triggering kick: latest successful kick at or before start, with no other
        # kick attempt in between and not already claimed by an earlier avalanche
        r["kick_id"] = -1
        i = np.searchsorted(kick_steps, s0, "right") - 1
        if i >= 0:
            ks = int(kick_steps[i])
            later_attempt = np.any((all_attempt_steps > ks) & (all_attempt_steps <= s0))
            if not later_attempt and ok_kicks[i]["kick_index"] not in claimed:
                r["kick_id"] = int(ok_kicks[i]["kick_index"])
                claimed.add(r["kick_id"])
        prev_end = s1
    return table


def run_avalanche_simulation(cfg: AvalancheRunConfig, out_dir: Optional[Path] = None,
                             progress: bool = False) -> Dict:
    t_wall = time.time()
    cfg.validate()
    rcfg = cfg.reservoir
    res = BiologicalEIReservoir(rcfg, device=cfg.device)
    if rcfg.input.n_inputs:
        raise ValueError("avalanche analysis is task-free: input.n_inputs must be 0")
    T = cfg.n_steps()
    NE = rcfg.N_E
    dt = rcfg.dt_ms
    st = res.reset_state(1)
    tgt = {"E": np.arange(NE), "I": np.arange(NE, rcfg.N), "all": np.arange(rcfg.N)}[cfg.kick.target_population]
    ctrl = KickController(cfg.kick, dt, tgt, numpy_rng(res.seeds["kick"]), cfg.kick.target_population)
    tracker = AvalancheTracker(cfg.primary_segmentation, dt)
    spikes_log = SpikeEventLogger(NE, dt)
    pop = PopulationActivityLogger(T)
    safety_rows: List[Dict] = []
    primary_online: List[Dict] = []
    pending_now = 0
    seg = cfg.primary_segmentation
    report_every = max(T // 20, 1)
    pre_window = cfg.steps(100.0) if cfg.steps(100.0) > 0 else 1   # "activity immediately before termination": last 100 ms
    termination = None
    safety_dur = cfg.steps(cfg.safety.max_duration_ms, "safety.max_duration_ms")

    for t in range(T):
        kick = None
        rec = None
        if ctrl.eligible(t, tracker.active, pending_now):
            refr = (st.neurons.ref[0, torch.as_tensor(tgt)] > 0).cpu().numpy()

            def state_of(i):
                return {"v": float(st.neurons.v[0, i]), "w": float(st.neurons.w[0, i]),
                        "ref": float(st.neurons.ref[0, i])}
            rec = ctrl.attempt(t, refr, {"v": np.nan, "w": np.nan, "ref": np.nan})
            rec.update({f"state_before_{k}": v for k, v in state_of(rec["target_neuron_id"]).items()})
            if rec["success"]:
                kick = torch.zeros(1, rcfg.N, dtype=res.dtype)
                kick[0, rec["target_neuron_id"]] = cfg.kick.amplitude
                pop.kick_indicator[t] = 1
        spikes = res.step(st, kick=kick)
        ids = torch.nonzero(spikes[0]).flatten().cpu().numpy()
        spikes_log.log(t, ids)
        nE = int((ids < NE).sum())
        nI = int(len(ids) - nE)
        pop.n_E[t], pop.n_I[t] = nE, nI
        pend = int(res.pending_spikes(st)[0].item())
        pop.pending[t] = pend
        if rec is not None and rec["success"]:
            rec["evoked_spike_at_kick_step"] = bool(rec["target_neuron_id"] in set(ids.tolist()))
        ctrl.observe(t, nE if cfg.kick.quiet_population == "E" else nE + nI)
        a = nE if seg.boundary_population == "E" else nE + nI
        s = nE if seg.size_population == "E" else nE + nI
        force = False
        if tracker.active or a > 0:
            pro = tracker.prospective(t, s)
            force = pro["size"] > cfg.safety.max_size_spikes or pro["duration"] > safety_dur
        closed = tracker.update(t, a, s, pend, force_close=force)
        if closed is not None:
            primary_online.append(closed)
            if closed["hit_safety_cutoff"]:
                safety_rows.append({"step": t, "time_ms": t * dt, "avalanche_id": closed["avalanche_id"],
                                    "size": closed["size"], "duration_steps": closed["duration_steps"],
                                    "action": cfg.safety.action})
                if cfg.safety.action == "reset_state":
                    res.reset_dynamics_(st)
                    pend = 0
                elif cfg.safety.action == "terminate_run":
                    pop.avalanche_active[t] = 0
                    w0 = max(0, t + 1 - pre_window)
                    termination = {
                        "run_status": "runaway_terminated",
                        "termination_step": int(t), "termination_time_ms": float(t * dt),
                        "completed_steps": int(t + 1), "completed_duration_ms": float((t + 1) * dt),
                        "planned_steps": int(T), "planned_duration_ms": float(cfg.duration_ms),
                        "triggering_criterion": ("size" if closed["size"] > cfg.safety.max_size_spikes else "duration"),
                        "triggering_avalanche_size": int(closed["size"]),
                        "triggering_avalanche_duration_ms": float(closed["duration_steps"] * dt),
                        "criterion": {"max_size_E_spikes": cfg.safety.max_size_spikes,
                                      "max_duration_ms": cfg.safety.max_duration_ms},
                        "pre_termination_window_ms": float((t + 1 - w0) * dt),
                        "pre_termination_E_spikes_per_step": float(pop.n_E[w0:t + 1].mean()),
                        "pre_termination_I_spikes_per_step": float(pop.n_I[w0:t + 1].mean()),
                        "last_step_E_spikes": int(nE), "last_step_I_spikes": int(nI),
                        "state_reset_before_termination": False,
                    }
                    break
        pending_now = pend
        pop.avalanche_active[t] = int(tracker.active)
        if progress and t % report_every == 0:
            print(f"step {t}/{T}  E={int(pop.n_E[:t+1].sum())} I={int(pop.n_I[:t+1].sum())} "
                  f"avalanches={len(primary_online)}", flush=True)
    if termination is None:
        primary_online += tracker.finish(T - 1)
        termination = {"run_status": "completed_normal", "completed_steps": int(T),
                       "completed_duration_ms": float(T * dt), "planned_steps": int(T),
                       "planned_duration_ms": float(cfg.duration_ms)}
    else:
        pop.truncate(termination["completed_steps"])
    sim_seconds = time.time() - t_wall

    events = spikes_log.arrays()
    forced = [r["step"] for r in safety_rows]
    n_E, n_I = pop.n_E.astype(np.int64), pop.n_I.astype(np.int64)
    burn = cfg.steps(cfg.burn_in_ms)
    kicks = ctrl.table()
    tables: Dict[str, List[Dict]] = {}
    for sd in [cfg.primary_segmentation, *cfg.secondary_segmentations]:
        a, s = _definition_arrays(sd, n_E, n_I)
        tab = segment_avalanches(a, s, pop.pending, sd, dt, forced_close_steps=forced)
        tables[sd.name] = enrich_avalanches(tab, n_E, n_I, events, kicks, burn, dt)
    prim = tables[cfg.primary_segmentation.name]
    replay_ok = [{k: r[k] for k in primary_online[0]} for r in prim] == primary_online if primary_online else not prim
    if not replay_ok:
        raise RuntimeError("offline replay of the primary definition differs from the online tracker")

    # link kicks -> avalanches (primary), first-spike latency; label events
    by_kick = {r["kick_id"]: r["avalanche_id"] for r in prim if r["kick_id"] >= 0}
    for k in kicks:
        k["avalanche_id_triggered"] = int(by_kick.get(k["kick_index"], -1))
        if k["success"]:
            m = (events["neuron_id"] == k["target_neuron_id"]) & (events["step"] >= k["simulation_step"])
            k["first_spike_latency_steps"] = int(events["step"][m][0] - k["simulation_step"]) if m.any() else -1
    starts = np.array([r["start_step"] for r in prim], dtype=np.int64)
    ends = np.array([r["end_step"] for r in prim], dtype=np.int64)
    aid = np.full(len(events["step"]), -1, np.int32)
    kid = np.full(len(events["step"]), -1, np.int32)
    if len(starts):
        j = np.searchsorted(starts, events["step"], "right") - 1
        inside = (j >= 0) & (events["step"] <= ends[np.maximum(j, 0)])
        aid[inside] = j[inside]
        kmap = np.array([r["kick_id"] for r in prim], dtype=np.int32)
        kid[inside] = kmap[j[inside]]
    events["avalanche_id"] = aid
    events["kick_id"] = kid

    counts = np.bincount(events["neuron_id"], minlength=rcfg.N)
    an = events["step"] >= burn
    counts_an = np.bincount(events["neuron_id"][an], minlength=rcfg.N)
    metrics = {
        "definition_note": "R_total = E/I total spikes; R_per_neuron = (E/N_E)/(I/N_I); "
                           "structural N_E/N_I is reported separately and is not an activity ratio",
        "full_run": activity_metrics(n_E, n_I, NE, rcfg.N_I, dt, counts),
        "analysis_interval": {"start_step": burn, **activity_metrics(n_E[burn:], n_I[burn:], NE, rcfg.N_I, dt, counts_an)},
        "n_kick_attempts": len(kicks), "n_kicks_successful": int(sum(k["success"] for k in kicks)),
        "n_safety_events": len(safety_rows),
        "run_status": termination["run_status"],
        "termination": termination,
        "n_avalanches": {name: len(t_) for name, t_ in tables.items()},
        "simulation_wall_seconds": sim_seconds,
    }
    result = {"config": cfg, "reservoir": res, "events": events, "population": pop.arrays(), "kicks": kicks,
              "safety": safety_rows, "avalanches": tables, "metrics": metrics}
    if out_dir is not None:
        write_run(out_dir, result)
    return result


def write_run(out_dir: Path, result: Dict) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = result["config"]
    save_config(cfg, out / "config.yaml")
    res = result["reservoir"]
    fp = res.fingerprint()
    write_json(fp, out / "reservoir_fingerprint.json")
    SpikeEventLogger.save(out / "spike_events.npz", result["events"])
    np.savez_compressed(out / "population_activity.npz", **result["population"])
    save_table(result["kicks"], out / "kick_events.csv", KICK_COLUMNS)
    save_table(result["safety"], out / "safety_events.csv",
               ["step", "time_ms", "avalanche_id", "size", "duration_steps", "action"])
    for name, tab in result["avalanches"].items():
        pd.DataFrame(tab).to_csv(out / f"avalanches_{name}.csv", index=False)
    write_json(result["metrics"], out / "activity_metrics.json")
    write_json({
        "protocol_label": cfg.protocol_label,
        "protocol_status": "RECONSTRUCTED: original NPRC E/I avalanche source is missing",
        "semantics": cfg.reservoir.semantics_label,
        "n_steps": cfg.n_steps(), "dt_ms": cfg.reservoir.dt_ms, "duration_ms": cfg.duration_ms,
        "seeds_resolved": res.seeds,
        "reservoir_id": fp["reservoir_id"],
        "environment": environment(),
        "files": sorted(p.name for p in out.iterdir()),
        "config": to_dict(cfg),
    }, out / "manifest.json")
