"""Example configurations (new in the shareable snapshot; not copied from the research repository).

The files under configs/ are demonstrations, not scientific protocols.  These tests only check
that they parse with the shipped schema and drive the shipped code end to end at tiny scale.
"""

from __future__ import annotations

import json
from pathlib import Path

import torch

from snnrc.config import AvalancheRunConfig, ReservoirConfig, load_config
from snnrc.criticality.analysis import analyze_run
from snnrc.criticality.simulate import run_avalanche_simulation
from snnrc.reservoir import BiologicalEIReservoir
from snnrc.sweep.manifest import build_run_config, enumerate_conditions, load_manifest
from snnrc.sweep.runner import run_sweep

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
RES = CONFIGS / "reservoir/example_b0_small.yaml"
RUN = CONFIGS / "avalanche/example_b0_small_run.yaml"
SWEEP = CONFIGS / "avalanche/example_sweep_manifest.yaml"


def test_example_reservoir_config_builds_corrected_dale_reservoir():
    cfg = load_config(ReservoirConfig, RES).validate()
    assert cfg.semantics_label == "corrected"
    res = BiologicalEIReservoir(cfg)
    fp = res.fingerprint()
    assert fp["dale_violations"] == 0
    assert fp["blocks"]["E_to_E"]["sign"] == 1.0 and fp["blocks"]["I_to_E"]["sign"] == -1.0
    a = res.run(n_steps=200, record_voltage=True)
    b = res.run(n_steps=200, record_voltage=True)
    assert a["spikes"].shape == (1, 200, cfg.N) and a["voltage"].shape == (1, 200, cfg.N)
    assert torch.equal(a["spikes"], b["spikes"]) and torch.equal(a["voltage"], b["voltage"])


def test_example_avalanche_config_runs_and_analyses(tmp_path):
    cfg = load_config(AvalancheRunConfig, RUN).validate()
    cfg.duration_ms, cfg.burn_in_ms = 600.0, 100.0
    out = tmp_path / "run"
    run_avalanche_simulation(cfg, out)
    summ = analyze_run(out, n_boot_ci=3, n_boot_gof=3, min_tail=5, min_distinct=3)
    assert set(summ) >= {"definitions", "activity"}
    assert (out / "spike_events.npz").exists() and (out / "analysis_summary.json").exists()


def test_example_sweep_manifest_enumerates_and_runs(tmp_path):
    m = load_manifest(SWEEP)
    conds = enumerate_conditions(m)
    assert [c.condition_id for c in conds] == ["B0_g06.00_s0", "B0_g07.00_s0", "B1_g06.00_s0", "B1_g07.00_s0"]
    assert build_run_config(m, conds[0]).reservoir.N == 100
    st = run_sweep(SWEEP, tmp_path / "sweep", max_workers=2, only=["B0_g06.00_s0"], duration_ms=600.0, mode="test")
    assert st["completed"] == ["B0_g06.00_s0"] and st["failed"] == []
    marker = json.loads((tmp_path / "sweep/runs/B0_g06.00_s0/COMPLETED.json").read_text())
    assert marker["run_status"] in {"completed_normal", "runaway_terminated"}
