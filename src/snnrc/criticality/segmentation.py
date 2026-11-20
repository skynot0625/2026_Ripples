"""Replayable avalanche segmentation (one pure rule, used online and offline).

Inputs per step ``t``: boundary activity ``a_t`` (spikes of the boundary
population), size increment ``s_t`` (spikes of the size population), and
``pending_t`` (spikes still in the delay ring after step t).

Rule (``SegmentationConfig``):

* **start**: first step with ``a_t > 0`` while no avalanche is active;
* **continuation**: any step with ``a_t > 0`` resets the silence counter;
  ``s_t`` is added to the size on every step while active;
* **close**: after ``silence >= close_silence_ms / dt`` consecutive steps with
  ``a_t == 0`` *and* (if ``require_no_pending``) ``pending_t == 0``.  Pending
  spikes keep the avalanche open; activity resuming before closure continues it.
* ``end_step`` = last step with ``a_t > 0``; ``duration_steps = end - start + 1``;
  ``close_step`` = step at which the closing criterion was met.
* **forced close** (safety cutoff) at step t: counts of step t are included,
  then the avalanche closes with ``hit_safety_cutoff=True``.
* an avalanche still open at the end of the record is emitted with
  ``closed_normally=False``.

:func:`segment_avalanches` literally runs :class:`AvalancheTracker` over the
saved arrays, so the online (simulation-time) and offline (replay) tables are
identical by construction.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

import numpy as np

from ..config import SegmentationConfig, ms_to_steps


class AvalancheTracker:
    def __init__(self, cfg: SegmentationConfig, dt_ms: float) -> None:
        self.close_steps = ms_to_steps(cfg.close_silence_ms, dt_ms, f"{cfg.name}.close_silence_ms")
        if self.close_steps < 1:
            raise ValueError("close_silence_ms must correspond to at least one step")
        self.cfg = cfg
        self.next_id = 0
        self.active = False
        self._reset_current()

    def _reset_current(self) -> None:
        self.start = -1
        self.last_active = -1
        self.size = 0
        self.peak = 0
        self.silence = 0

    def prospective(self, t: int, s: int) -> Dict[str, int]:
        """Size/duration the current avalanche would have after adding step t."""

        if self.active:
            return {"size": self.size + s, "duration": t - self.start + 1}
        return {"size": s, "duration": 1}

    def _emit(self, close_step: int, normal: bool, safety: bool) -> Dict:
        rec = {
            "avalanche_id": self.next_id,
            "start_step": int(self.start),
            "end_step": int(self.last_active),
            "close_step": int(close_step),
            "duration_steps": int(self.last_active - self.start + 1),
            "size": int(self.size),
            "peak_per_step": int(self.peak),
            "closed_normally": bool(normal),
            "hit_safety_cutoff": bool(safety),
        }
        self.next_id += 1
        self.active = False
        self._reset_current()
        return rec

    def update(self, t: int, a: int, s: int, pending: int, force_close: bool = False) -> Optional[Dict]:
        if not self.active:
            if a > 0:
                self.active = True
                self.start = self.last_active = t
                self.size, self.peak, self.silence = s, a, 0
            elif force_close:
                return None
            else:
                return None
        else:
            self.size += s
            if a > 0:
                self.last_active = t
                self.silence = 0
                self.peak = max(self.peak, a)
            else:
                self.silence += 1
        if self.active and force_close:
            return self._emit(t, normal=False, safety=True)
        if self.active and a == 0 and self.silence >= self.close_steps:
            if not self.cfg.require_no_pending or pending == 0:
                return self._emit(t, normal=True, safety=False)
        return None

    def finish(self, t_last: int) -> List[Dict]:
        return [self._emit(t_last, normal=False, safety=False)] if self.active else []


def segment_avalanches(a: np.ndarray, s: np.ndarray, pending: np.ndarray, cfg: SegmentationConfig,
                       dt_ms: float, forced_close_steps: Iterable[int] = ()) -> List[Dict]:
    a = np.asarray(a); s = np.asarray(s); pending = np.asarray(pending)
    if not (len(a) == len(s) == len(pending)):
        raise ValueError("a, s, pending must have equal length")
    forced = set(int(x) for x in forced_close_steps)
    tr = AvalancheTracker(cfg, dt_ms)
    out: List[Dict] = []
    a_l, s_l, p_l = a.tolist(), s.tolist(), pending.tolist()
    for t in range(len(a_l)):
        rec = tr.update(t, int(a_l[t]), int(s_l[t]), int(p_l[t]), force_close=t in forced)
        if rec is not None:
            out.append(rec)
    out += tr.finish(len(a_l) - 1)
    return out
