"""Raw event loggers (machine-readable; plots are derived from these files only).

``spike_events.npz``      step, time_ms, neuron_id, population (0=E, 1=I),
                          avalanche_id (primary definition, -1 outside), kick_id (-1 none)
``population_activity.npz`` per step: n_E, n_I, n_total, pending (ring occupancy
                          after the step), kick_indicator, avalanche_active (primary)
``kick_events.csv``       one row per attempted kick (successful and failed)
``safety_events.csv``     one row per safety cutoff / reset
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd


class SpikeEventLogger:
    def __init__(self, N_E: int, dt_ms: float) -> None:
        self.N_E, self.dt_ms = N_E, dt_ms
        self._steps: List[np.ndarray] = []
        self._ids: List[np.ndarray] = []

    def log(self, t: int, neuron_ids: np.ndarray) -> None:
        if len(neuron_ids):
            self._steps.append(np.full(len(neuron_ids), t, dtype=np.int64))
            self._ids.append(np.asarray(neuron_ids, dtype=np.int32))

    def arrays(self) -> Dict[str, np.ndarray]:
        step = np.concatenate(self._steps) if self._steps else np.zeros(0, np.int64)
        nid = np.concatenate(self._ids) if self._ids else np.zeros(0, np.int32)
        return {"step": step, "time_ms": step * self.dt_ms, "neuron_id": nid,
                "population": (nid >= self.N_E).astype(np.uint8)}

    @staticmethod
    def save(path: Path, arrays: Dict[str, np.ndarray]) -> None:
        np.savez_compressed(path, **arrays)


class PopulationActivityLogger:
    FIELDS = ("n_E", "n_I", "n_total", "pending", "kick_indicator", "avalanche_active")

    def __init__(self, n_steps: int) -> None:
        self.n_E = np.zeros(n_steps, np.int32)
        self.n_I = np.zeros(n_steps, np.int32)
        self.pending = np.zeros(n_steps, np.int32)
        self.kick_indicator = np.zeros(n_steps, np.int8)
        self.avalanche_active = np.zeros(n_steps, np.int8)

    def truncate(self, n: int) -> None:
        """Keep only the first ``n`` steps (early-terminated runs)."""

        for name in ("n_E", "n_I", "pending", "kick_indicator", "avalanche_active"):
            setattr(self, name, getattr(self, name)[:n])

    def arrays(self) -> Dict[str, np.ndarray]:
        return {"n_E": self.n_E, "n_I": self.n_I, "n_total": self.n_E + self.n_I, "pending": self.pending,
                "kick_indicator": self.kick_indicator, "avalanche_active": self.avalanche_active}

    def save(self, path: Path) -> None:
        np.savez_compressed(path, **self.arrays())


def save_table(rows: List[dict], path: Path, columns: List[str] | None = None) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=columns) if (rows or columns) else pd.DataFrame()
    df.to_csv(path, index=False)
    return df


__all__ = ["SpikeEventLogger", "PopulationActivityLogger", "save_table"]
