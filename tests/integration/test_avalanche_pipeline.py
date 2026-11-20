"""Integration: short kick-driven run -> saved raw events -> offline replay."""

import json

import numpy as np
import pandas as pd
import pytest

from snnrc.criticality.analysis import analyze_run, load_run
from snnrc.criticality.segmentation import segment_avalanches
from snnrc.criticality.simulate import run_avalanche_simulation
from snnrc.presets import avalanche_pilot


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    cfg = avalanche_pilot("B1", g_global=7.0, master_seed=1, duration_ms=1500.0)
    cfg.burn_in_ms = 100.0
    cfg.kick.quiet_before_kick_ms = 30.0
    cfg.kick.cooldown_ms = 30.0
    out = tmp_path_factory.mktemp("aval")
    run_avalanche_simulation(cfg, out)
    return out


def test_required_artifacts_exist(run_dir):
    for name in ["config.yaml", "manifest.json", "reservoir_fingerprint.json", "spike_events.npz",
                 "population_activity.npz", "kick_events.csv", "safety_events.csv",
                 "avalanches_primary_E_seg_E_size.csv", "avalanches_A_total_seg_E_size.csv",
                 "avalanches_B_total_seg_total_size.csv", "activity_metrics.json"]:
        assert (run_dir / name).exists(), name


def test_raw_events_consistent_with_population_counts(run_dir):
    run = load_run(run_dir)
    ev, pop = run["events"], run["population"]
    T = len(pop["n_E"])
    nE = np.bincount(ev["step"][ev["population"] == 0], minlength=T)
    nI = np.bincount(ev["step"][ev["population"] == 1], minlength=T)
    assert np.array_equal(nE, pop["n_E"]) and np.array_equal(nI, pop["n_I"])
    assert np.all((ev["neuron_id"] < 800) == (ev["population"] == 0))
    np.testing.assert_allclose(ev["time_ms"], ev["step"] * 1.0)


def test_offline_replay_reproduces_saved_tables(run_dir):
    run = load_run(run_dir)
    cfg = run["config"]
    pop = run["population"]
    ev = run["events"]
    T = len(pop["n_E"])
    nE = np.bincount(ev["step"][ev["population"] == 0], minlength=T)   # rebuilt from raw events
    nI = np.bincount(ev["step"][ev["population"] == 1], minlength=T)
    forced = run["safety"]["step"].to_numpy() if len(run["safety"]) else ()
    for seg in [cfg.primary_segmentation, *cfg.secondary_segmentations]:
        a = nE if seg.boundary_population == "E" else nE + nI
        s = nE if seg.size_population == "E" else nE + nI
        replay = segment_avalanches(a, s, pop["pending"], seg, cfg.reservoir.dt_ms, forced_close_steps=forced)
        saved = pd.read_csv(run_dir / f"avalanches_{seg.name}.csv")
        assert len(replay) == len(saved)
        assert [r["size"] for r in replay] == saved["size"].tolist()
        assert [r["start_step"] for r in replay] == saved["start_step"].tolist()


def test_kicks_target_E_and_are_logged(run_dir):
    k = pd.read_csv(run_dir / "kick_events.csv")
    assert len(k) > 0
    assert set(k["target_population"]) == {"E"} and (k["target_neuron_id"] < 800).all()
    assert {"kick_index", "simulation_step", "time_ms", "amplitude", "success", "failure_reason",
            "state_before_v", "first_spike_latency_steps", "avalanche_id_triggered"} <= set(k.columns)


def test_primary_avalanches_link_to_kicks_and_events(run_dir):
    av = pd.read_csv(run_dir / "avalanches_primary_E_seg_E_size.csv")
    run = load_run(run_dir)
    ev = run["events"]
    assert len(av) > 0
    # E spikes labelled with an avalanche id sum to that avalanche's E size
    for _, r in av.head(5).iterrows():
        mask = (ev["avalanche_id"] == r["avalanche_id"]) & (ev["population"] == 0)
        assert mask.sum() == r["size_E_spikes"] == r["size"]
    assert {"kick_id", "size_E_spikes", "size_I_spikes", "size_total_spikes", "unique_E_neurons",
            "unique_I_neurons", "inter_avalanche_interval_steps", "in_burn_in"} <= set(av.columns)


def test_analysis_produces_fits_metrics_and_raster(run_dir):
    summary = analyze_run(run_dir, n_boot_ci=10, n_boot_gof=5, min_tail=5, min_distinct=3, make_plots=True)
    m = json.loads((run_dir / "activity_metrics.json").read_text())
    assert {"R_total", "R_per_neuron", "E_rate_hz", "I_rate_hz"} <= set(m["analysis_interval"])
    assert (run_dir / "plots" / "raster_overview.png").exists()
    assert (run_dir / "plots" / "size_pmf_ccdf_primary_E_seg_E_size.png").exists()
    prim = summary["definitions"]["primary_E_seg_E_size"]
    assert prim["role"] == "primary" and "size_fit" in prim and "duration_fit" in prim
    assert (run_dir / "fit_duration_primary_E_seg_E_size.json").exists()
    assert (run_dir / "distribution_duration_primary_E_seg_E_size.csv").exists()
    assert "n_kicks_failed" in summary["kicks"]


def test_extra_definition_replayed_offline_without_rerun(run_dir):
    from snnrc.config import SegmentationConfig
    before = (run_dir / "spike_events.npz").stat().st_mtime_ns
    seg = SegmentationConfig("S_close1ms_E_seg_E_size", "E", "E", 1.0, True)
    summary = analyze_run(run_dir, n_boot_ci=5, n_boot_gof=3, min_tail=5, min_distinct=3, make_plots=False,
                          extra_definitions=[seg])
    assert summary["definitions"][seg.name]["role"] == "sensitivity"
    assert (run_dir / f"avalanches_{seg.name}.csv").exists()
    assert (run_dir / "spike_events.npz").stat().st_mtime_ns == before


def test_fingerprint_contents(run_dir):
    fp = json.loads((run_dir / "reservoir_fingerprint.json").read_text())
    assert fp["semantics"] == "corrected" and fp["family"] == "B1"
    assert fp["scaling"]["mode"] == "global" and fp["rho_applied"] == pytest.approx(7.0, rel=1e-6)
    assert fp["dale_violations"] == 0 and fp["N_E"] == 800 and fp["N_I"] == 200
    assert len(fp["reservoir_id"]) == 16 and len(fp["base_structure_id"]) == 16
