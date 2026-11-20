"""Spec tests: Dale E/I generator, orientation, and the two scaling modes."""

import numpy as np
import pytest
import torch

from snnrc.config import ScalingConfig
from snnrc.connectivity import build_dale_ei_structure, apply_scaling, spectral_radius
from snnrc.connectivity.dale_ei import sparse_global_weights
from snnrc.presets import nprc_b0, nprc_b1
from snnrc.reservoir import BiologicalEIReservoir


@pytest.fixture(scope="module")
def structure():
    return build_dale_ei_structure(nprc_b0(1.0, master_seed=3))


def test_blockwise_led_graph_matches_original_across_block_boundaries():
    cfg = nprc_b0(7.0, master_seed=3)
    cfg.N_E, cfg.N_I = 160, 40
    dense = apply_scaling(build_dale_ei_structure(cfg), cfg.scaling)
    actual = sparse_global_weights(cfg).toarray()
    np.testing.assert_array_equal(actual != 0, dense != 0)
    np.testing.assert_allclose(actual, dense, rtol=1e-8)


def test_dale_signs_per_presynaptic_column(structure):
    W0, NE = structure.W0, structure.N_E
    assert (W0[:, :NE] >= 0).all() and (W0[:, NE:] <= 0).all()
    assert (W0[:, :NE] > 0).any() and (W0[:, NE:] < 0).any()


def test_block_magnitudes_and_no_self_loops(structure):
    W0, NE = structure.W0, structure.N_E
    assert np.all(np.diag(W0) == 0)
    assert set(np.unique(W0[:NE, :NE])) <= {0.0, 1.0}
    assert set(np.unique(W0[NE:, :NE])) <= {0.0, 1.0}
    assert set(np.unique(W0[:NE, NE:])) <= {0.0, -4.0}
    assert set(np.unique(W0[NE:, NE:])) <= {0.0, -3.0}      # 4 * 0.75


def test_density_close_to_legacy_ssc(structure):
    NE = structure.N_E
    dens = (structure.W0[:NE, :NE] != 0).mean()
    assert 0.02 < dens < 0.06           # legacy SSC seed 42: 0.0375


def test_spatial_probability_decreases_with_distance(structure):
    NE = structure.N_E
    d = structure.distance[:NE, :NE]
    conn = structure.W0[:NE, :NE] != 0
    near = conn[(d > 0) & (d < 0.1)].mean()
    far = conn[d > 0.5].mean()
    assert near > 3 * far


def test_fitness_increases_out_degree(structure):
    NE = structure.N_E
    out_deg = (structure.W0[:, :NE] != 0).sum(axis=0)          # column = presynaptic
    r = np.corrcoef(structure.fitness[:NE], out_deg)[0, 1]
    assert r > 0.5


def test_global_scaling_sets_spectral_radius_and_scales_all_blocks(structure):
    W1 = apply_scaling(structure, ScalingConfig("global", g_global=1.0))
    W7 = apply_scaling(structure, ScalingConfig("global", g_global=7.0))
    assert spectral_radius(W7) == pytest.approx(7.0, rel=1e-9)
    np.testing.assert_allclose(W7, 7.0 * W1, rtol=1e-12)
    assert np.array_equal(W7 != 0, structure.W0 != 0)          # paired topology


def test_excitatory_scaling_changes_only_E_columns(structure):
    NE = structure.N_E
    a = apply_scaling(structure, ScalingConfig("excitatory", J_E_exc=2.0, J_I=8.0))
    b = apply_scaling(structure, ScalingConfig("excitatory", J_E_exc=5.0, J_I=8.0))
    np.testing.assert_allclose(b[:, NE:], a[:, NE:])
    np.testing.assert_allclose(b[:, :NE], 2.5 * a[:, :NE])


def test_excitatory_scaling_identity_bridge(structure):
    g = 6.3
    glob = apply_scaling(structure, ScalingConfig("global", g_global=g))
    exc = apply_scaling(structure, ScalingConfig("excitatory", J_E_exc=g, J_I=4.0 * g))
    np.testing.assert_allclose(exc, glob, rtol=1e-12)


def test_topology_seed_independent_of_other_seeds():
    a = nprc_b0(1.0); a.seeds.topology = 11; a.seeds.neuron_params = 1; a.seeds.noise = 1
    b = nprc_b0(1.0); b.seeds.topology = 11; b.seeds.neuron_params = 2; b.seeds.noise = 2
    c = nprc_b0(1.0); c.seeds.topology = 12
    assert np.array_equal(build_dale_ei_structure(a).W0, build_dale_ei_structure(b).W0)
    assert not np.array_equal(build_dale_ei_structure(a).W0, build_dale_ei_structure(c).W0)


def test_current_equals_W_at_spikes_single_edge():
    """W[post, pre] and current = W @ spikes, checked through one reservoir step."""

    cfg = nprc_b0(1.0, master_seed=1)
    cfg.noise.sigma = 0.0
    res = BiologicalEIReservoir(cfg)
    W = torch.zeros_like(res.W)
    pre, post = 3, 10
    W[post, pre] = 0.4
    res.set_recurrent_weights(W)
    st = res.reset_state(1)
    st.buffer[1 % st.buffer.shape[0], 0, pre] = 1.0     # spike of `pre` arriving at step 1
    res.step(st)                                         # t = 0
    res.step(st)                                         # t = 1
    assert st.last_synaptic_current[0, post].item() == pytest.approx(0.4)
    assert st.last_synaptic_current[0].abs().sum().item() == pytest.approx(0.4)


def test_inhibition_is_subtracted_exactly_once():
    cfg = nprc_b0(1.0, master_seed=1)
    cfg.noise.sigma = 0.0
    res = BiologicalEIReservoir(cfg)
    NE = cfg.N_E
    W = torch.zeros_like(res.W)
    W[5, NE + 2] = -0.7
    res.set_recurrent_weights(W)
    st = res.reset_state(1)
    st.buffer[1, 0, NE + 2] = 1.0
    res.step(st); res.step(st)
    assert st.last_synaptic_current[0, 5].item() == pytest.approx(-0.7)


def test_adaptation_assignment_exact_split_and_I_non_adaptive():
    res = BiologicalEIReservoir(nprc_b0(1.0, master_seed=2))
    b = res.neurons.b.cpu().numpy()
    NE = 800
    assert int(np.isclose(b[:NE], 0.05).sum()) == 480 and int(np.isclose(b[:NE], 0.20).sum()) == 320
    assert np.all(b[NE:] == 0.0)
    tw = res.neurons.tau_w_ms.cpu().numpy()
    assert 80 < np.median(tw[:NE][np.isclose(b[:NE], 0.05)]) < 140
    assert 250 < np.median(tw[:NE][np.isclose(b[:NE], 0.20)]) < 420


def test_b015_ablation_only_changes_strong_b():
    a = BiologicalEIReservoir(nprc_b0(1.0, "historical_b020", master_seed=2))
    b = BiologicalEIReservoir(nprc_b0(1.0, "historical_b015", master_seed=2))
    ba, bb = a.neurons.b.numpy(), b.neurons.b.numpy()
    strong = np.isclose(ba, 0.20)
    assert np.allclose(bb[strong], 0.15) and np.array_equal(ba[~strong], bb[~strong])
    assert torch.equal(a.W, b.W) and torch.equal(a.neurons.tau_w_ms, b.neurons.tau_w_ms)


def test_b1_delays_are_at_least_one_step():
    res = BiologicalEIReservoir(nprc_b1(1.0, master_seed=4))
    d = res.delay_steps.numpy()
    assert d.min() >= 1 and d.max() <= 8
    assert res.max_delay_steps == d.max()
