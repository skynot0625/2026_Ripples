"""Spec tests: B0 one-step recurrence, B1 delay semantics, noise and seeds, freezing."""

import numpy as np
import pytest
import torch

from snnrc.presets import nprc_b0, nprc_b1
from snnrc.reservoir import BiologicalEIReservoir


def quiet(cfg):
    cfg.noise.sigma = 0.0
    return cfg


def chain_reservoir(cfg, pre, post, w=5.0):
    res = BiologicalEIReservoir(quiet(cfg))
    W = torch.zeros_like(res.W)
    W[post, pre] = w
    res.set_recurrent_weights(W)
    return res


def run_with_kick(res, pre, T):
    st = res.reset_state(1)
    rec = []
    for t in range(T):
        kick = None
        if t == 0:
            kick = torch.zeros(1, res.N, dtype=res.dtype); kick[0, pre] = 5.0
        rec.append(res.step(st, kick=kick)[0].clone())
    return torch.stack(rec).numpy(), st


def test_b0_one_step_recurrence():
    res = chain_reservoir(nprc_b0(1.0), pre=2, post=7)
    S, _ = run_with_kick(res, 2, 5)
    assert np.flatnonzero(S[:, 2]).tolist() == [0]
    assert np.flatnonzero(S[:, 7]).tolist() == [1]


@pytest.mark.parametrize("d", [1, 3, 8])
def test_b1_delay_arrives_exactly_d_steps_later(d):
    res = chain_reservoir(nprc_b1(1.0), pre=2, post=7)
    delays = res.delay_steps.clone(); delays[2] = d
    res.set_delays(delays, max_delay_steps=8)
    S, _ = run_with_kick(res, 2, 25)
    assert np.flatnonzero(S[:, 7]).tolist() == [d]


def test_b1_ring_wraparound_over_many_cycles():
    """Repeated spikes far beyond the ring length still arrive exactly d steps later."""

    res = chain_reservoir(nprc_b1(1.0), pre=2, post=7, w=5.0)
    delays = res.delay_steps.clone().clamp(max=6); delays[2] = 5
    res.set_delays(delays, max_delay_steps=6)        # ring of 7 slots
    st = res.reset_state(1)
    kick_steps = [0, 4, 9, 23, 40]
    out = []
    for t in range(50):
        kick = None
        if t in kick_steps:
            kick = torch.zeros(1, res.N, dtype=res.dtype); kick[0, 2] = 5.0
        out.append(res.step(st, kick=kick)[0, 7].item())
    assert np.flatnonzero(out).tolist() == [k + 5 for k in kick_steps]


def test_multiple_spikes_accumulate_in_buffer():
    res = chain_reservoir(nprc_b1(1.0), pre=2, post=7, w=0.6)
    W = res.W.clone(); W[7, 3] = 0.6; res.set_recurrent_weights(W)
    delays = res.delay_steps.clone(); delays[2] = 4; delays[3] = 4
    res.set_delays(delays, max_delay_steps=8)
    st = res.reset_state(1)
    kick = torch.zeros(1, res.N, dtype=res.dtype); kick[0, [2, 3]] = 5.0
    res.step(st, kick=kick)
    assert res.pending_spikes(st).item() == 2
    for _ in range(4):
        s = res.step(st)
    assert s[0, 7].item() is True          # 0.6 + 0.6 >= 1 only if both arrive


def test_pending_counter_b0_and_b1():
    res = chain_reservoir(nprc_b0(1.0), pre=2, post=7, w=0.0)
    st = res.reset_state(1)
    kick = torch.zeros(1, res.N, dtype=res.dtype); kick[0, 2] = 5.0
    res.step(st, kick=kick)
    assert res.pending_spikes(st).item() == 1           # emitted at t, delivered at t+1
    res.step(st)
    assert res.pending_spikes(st).item() == 0


def test_shared_noise_stream_reproducible():
    def run(seed):
        cfg = nprc_b0(7.0, master_seed=5); cfg.seeds.noise = seed; cfg.noise.sigma = 0.3
        res = BiologicalEIReservoir(cfg)
        st = res.reset_state(1)
        return torch.stack([res.step(st)[0] for _ in range(50)])
    assert torch.equal(run(1), run(1))
    assert not torch.equal(run(1), run(2))


def test_per_sample_noise_independent_of_batch_composition():
    cfg = nprc_b0(1.0, master_seed=5); cfg.noise.sigma = 0.4; cfg.noise.mode = "per_sample"
    res = BiologicalEIReservoir(cfg)
    def run(keys):
        st = res.reset_state(len(keys), sample_keys=keys)
        return torch.stack([res.step(st) for _ in range(30)])
    both = run([10, 11])
    alone = run([11])
    assert torch.equal(both[:, 1], alone[:, 0])


def test_sigma_zero_is_deterministic_control():
    cfg = quiet(nprc_b0(1.0, master_seed=5))
    cfg.input.n_inputs = 3
    res = BiologicalEIReservoir(cfg)
    u = torch.rand(1, 40, 3, generator=torch.Generator().manual_seed(0), dtype=torch.float64)
    a = res.run(u)["spikes"]; b = res.run(u)["spikes"]
    assert torch.equal(a, b)


def test_input_projects_to_E_only():
    cfg = nprc_b0(1.0); cfg.input.n_inputs = 5
    res = BiologicalEIReservoir(cfg)
    assert res.W_in.shape == (1000, 5)
    assert torch.all(res.W_in[800:] == 0) and torch.any(res.W_in[:800] != 0)


def test_fixed_reservoir_gradient_isolation():
    from snnrc.readout import LinearClassificationReadout, readout_optimizer

    cfg = nprc_b0(7.0, master_seed=1); cfg.input.n_inputs = 4; cfg.noise.sigma = 0.0
    res = BiologicalEIReservoir(cfg)
    for t in res.tensors().values():
        assert not t.requires_grad
    u = torch.rand(2, 20, 4, dtype=torch.float64) * 3
    out = res.run(u)
    assert out["spikes"].grad_fn is None
    ro = LinearClassificationReadout(res.N, 3, dtype=torch.float64)
    opt = readout_optimizer(ro, res, lr=1e-2)
    assert {id(p) for g in opt.param_groups for p in g["params"]} == {id(p) for p in ro.parameters()}
    loss = torch.nn.functional.cross_entropy(ro(out["spikes"].to(torch.float64)), torch.tensor([0, 2]))
    loss.backward()
    assert all(p.grad is not None for p in ro.parameters())
    for t in res.tensors().values():
        assert t.grad is None
