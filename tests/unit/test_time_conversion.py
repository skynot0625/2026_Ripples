"""Spec tests: every biologically defined interval stays constant in ms when dt changes."""

import numpy as np
import pytest
import torch

from snnrc.config import KickConfig, SegmentationConfig, ms_to_steps
from snnrc.criticality.segmentation import segment_avalanches
from snnrc.presets import avalanche_historical_pilot, nprc_b1, nprc_historical_dt01
from snnrc.reservoir import BiologicalEIReservoir, KickController
from snnrc.reservoir.biological_ei import sample_delay_ms
from snnrc.seeding import resolve_seeds


def test_step_table_at_dt_0p1():
    cfg = avalanche_historical_pilot("B1", 7.0, 0, 10000.0)
    t = cfg.step_table()
    assert t["dt_ms"] == 0.1 and t["n_steps"] == 100_000
    assert t["refractory_E_steps"] == 10 and t["refractory_I_steps"] == 10
    assert t["quiet_before_kick_steps"] == 1000 and t["cooldown_steps"] == 1000
    assert t["burn_in_steps"] == 10_000 and t["safety_max_duration_steps"] == 200_000
    assert all(v == 50 for k, v in t.items() if k.startswith("close_silence_steps"))


def test_ms_to_steps_rejects_non_multiples():
    assert ms_to_steps(1.0, 0.1, "x") == 10
    with pytest.raises(ValueError):
        ms_to_steps(0.15, 0.1, "x")


def test_refractory_1ms_is_10_steps_in_dynamics():
    cfg = nprc_historical_dt01("B0", 1.0)
    res = BiologicalEIReservoir(cfg)
    assert int(res.neurons.refractory_steps[0]) == 10 and int(res.neurons.refractory_steps[-1]) == 10
    st = res.neurons.init_state(1)
    out = [bool(res.neurons.step(st, torch.full((1, res.N), 5.0, dtype=torch.float64))[0, 0]) for _ in range(34)]
    assert np.flatnonzero(out).tolist() == [0, 11, 22, 33]       # 1 ms = 10 blocked steps


def test_delays_sampled_in_ms_then_converted():
    a = nprc_b1(7.0, master_seed=0)
    b = nprc_historical_dt01("B1", 7.0, master_seed=0)
    seed = resolve_seeds(a.seeds)["delays"]
    assert seed == resolve_seeds(b.seeds)["delays"]
    ms = sample_delay_ms(a, seed)
    np.testing.assert_array_equal(ms, sample_delay_ms(b, seed))         # same ms for both dt
    ra, rb = BiologicalEIReservoir(a), BiologicalEIReservoir(b)
    np.testing.assert_array_equal(ra.delay_steps.numpy(), np.maximum(np.round(ms / 1.0), 1))
    np.testing.assert_array_equal(rb.delay_steps.numpy(), np.maximum(np.round(ms / 0.1), 1))
    assert np.all(np.abs(rb.delay_steps.numpy() * 0.1 - ms) <= 0.05 + 1e-9)
    assert rb.max_delay_steps == 80 or rb.max_delay_steps <= 80


def test_closure_5ms_is_50_steps_at_dt_0p1():
    a = np.r_[1, np.zeros(60, int), 1, np.zeros(60, int)]
    cfg = SegmentationConfig("p", "E", "E", 5.0, True)
    tab = segment_avalanches(a, a, np.zeros_like(a), cfg, 0.1)
    assert [r["close_step"] for r in tab] == [50, 111]
    b = np.r_[1, np.zeros(49, int), 1, np.zeros(60, int)]          # 49-step gap < 5 ms: one avalanche
    assert len(segment_avalanches(b, b, np.zeros_like(b), cfg, 0.1)) == 1


def test_quiet_and_cooldown_in_steps_at_dt_0p1():
    c = KickController(KickConfig(quiet_before_kick_ms=100.0, cooldown_ms=100.0), 0.1, np.arange(3),
                       np.random.default_rng(0))
    assert c.quiet_steps == 1000 and c.cooldown_steps == 1000


def test_same_base_structure_across_pairs():
    fps = [BiologicalEIReservoir(nprc_historical_dt01(f, g, 0)).fingerprint() for f in ("B0", "B1") for g in (7.0, 7.1)]
    assert len({fp["base_structure_id"] for fp in fps}) == 1
    assert len({fp["reservoir_id"] for fp in fps}) == 4
    assert {round(fp["rho_applied"], 9) for fp in fps} == {7.0, 7.1}


def test_noise_is_per_step_not_dt_scaled():
    cfg = nprc_historical_dt01("B0", 1.0)
    assert BiologicalEIReservoir(cfg).noise_std == 0.05


def test_signonly_control_differs_only_in_sign_flag_and_name():
    from snnrc.config import to_dict
    from snnrc.presets import avalanche_signonly_control

    a = to_dict(avalanche_historical_pilot("B1", 7.0, 0))
    b = to_dict(avalanche_signonly_control("B1", 7.0, 0))
    a["reservoir"].pop("name"); b["reservoir"].pop("name")
    a.pop("protocol_label"); b.pop("protocol_label")
    la, lb = a["reservoir"].pop("legacy"), b["reservoir"].pop("legacy")
    assert a == b
    assert lb == {**la, "double_negative_inhibition": True}
    ra = BiologicalEIReservoir(avalanche_historical_pilot("B1", 7.0, 0).reservoir)
    rb = BiologicalEIReservoir(avalanche_signonly_control("B1", 7.0, 0).reservoir)
    assert torch.equal(ra.W, rb.W) and torch.equal(ra.delay_steps, rb.delay_steps)
    assert torch.equal(rb.W_applied[:, 800:], -ra.W[:, 800:]) and torch.equal(rb.W_applied[:, :800], ra.W[:, :800])
