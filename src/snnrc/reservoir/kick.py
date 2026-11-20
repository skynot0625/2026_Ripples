"""Silence-triggered kick protocol (a reconstruction; the original protocol source is unavailable).

Eligibility at the *start* of step ``t`` uses only information up to ``t-1``:

1. ``quiet_steps`` consecutive steps (ending at t-1) without spikes in the
   ``quiet_population`` (``E`` by default; counted from the start of the run);
2. no avalanche active (primary online tracker) if required;
3. no spikes pending in the delay ring if required;
4. ``t - last_attempt_step >= cooldown_steps`` (cooldown applies to every
   attempt, successful or failed).

Every attempt is logged.  If the uniformly drawn target is refractory the
attempt is logged as failed (``on_refractory_target='log_failed'``) or a
non-refractory target is drawn instead (``'retarget'``).  Kicks are never
silently lost.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from ..config import KickConfig, ms_to_steps


class KickController:
    def __init__(self, cfg: KickConfig, dt_ms: float, target_ids: np.ndarray, rng: np.random.Generator,
                 target_population_label: str = "E") -> None:
        self.cfg = cfg
        self.quiet_steps = ms_to_steps(cfg.quiet_before_kick_ms, dt_ms, "quiet_before_kick_ms")
        self.cooldown_steps = ms_to_steps(cfg.cooldown_ms, dt_ms, "cooldown_ms")
        self.dt_ms = dt_ms
        self.targets = np.asarray(target_ids)
        self.rng = rng
        self.label = target_population_label
        self.quiet_count = 0
        self.last_attempt: Optional[int] = None
        self.records: List[Dict[str, Any]] = []

    def observe(self, t: int, quiet_activity: int) -> None:
        self.quiet_count = 0 if quiet_activity > 0 else self.quiet_count + 1

    def eligible(self, t: int, avalanche_active: bool, pending: int) -> bool:
        if self.quiet_count < self.quiet_steps:
            return False
        if self.cfg.require_no_active_avalanche and avalanche_active:
            return False
        if self.cfg.require_no_pending_spikes and pending > 0:
            return False
        if self.last_attempt is not None and t - self.last_attempt < self.cooldown_steps:
            return False
        return True

    def attempt(self, t: int, refractory: np.ndarray, state_before: Dict[str, float]) -> Dict[str, Any]:
        """Choose a target; ``refractory`` is a bool array over ``target_ids``."""

        refractory = np.asarray(refractory, bool)
        k = int(self.rng.integers(len(self.targets)))
        success, reason = True, ""
        if refractory[k]:
            if self.cfg.on_refractory_target == "retarget" and (~refractory).any():
                free = np.flatnonzero(~refractory)
                k = int(free[self.rng.integers(len(free))])
            else:
                success, reason = False, "target_refractory"
        rec = {
            "kick_index": len(self.records),
            "simulation_step": int(t),
            "time_ms": float(t * self.dt_ms),
            "target_population": self.label,
            "target_neuron_id": int(self.targets[k]),
            "amplitude": float(self.cfg.amplitude),
            "application": self.cfg.application,
            "success": bool(success),
            "failure_reason": reason,
            **{f"state_before_{key}": float(val) for key, val in state_before.items()},
            "evoked_spike_at_kick_step": False,
            "first_spike_latency_steps": -1,
            "avalanche_id_triggered": -1,
        }
        self.records.append(rec)
        self.last_attempt = t
        return rec

    def table(self) -> List[Dict[str, Any]]:
        return self.records
