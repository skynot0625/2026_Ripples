"""Spec tests: neuron dynamics of the shared core (snnrc.neurons)."""

import math

import pytest
import torch

from snnrc.neurons import LIF, AdaptiveLIF


def drive(neuron, currents, state=None):
    state = state or neuron.init_state(1)
    out = []
    for c in currents:
        s = neuron.step(state, torch.full((1, neuron.n), float(c), dtype=torch.float64))
        out.append(s[0].to(torch.int64).tolist())
    return out, state


def test_leak_with_zero_input():
    n = LIF(1, tau_m_ms=20.0, dt_ms=1.0, dtype=torch.float64)
    st = n.init_state(1)
    st.v[:] = 0.5
    n.step(st, torch.zeros(1, 1, dtype=torch.float64))
    assert st.v.item() == pytest.approx(0.5 * math.exp(-1 / 20))


def test_threshold_crossing_and_hard_reset():
    n = LIF(1, tau_m_ms=20.0, dt_ms=1.0, threshold=1.0, v_reset=0.0, reset_mode="hard", dtype=torch.float64)
    spikes, st = drive(n, [0.999])
    assert spikes == [[0]]
    spikes, st = drive(n, [0.5], st)       # 0.999*a + 0.5 >= 1
    assert spikes == [[1]] and st.v.item() == 0.0


def test_subtractive_reset_keeps_overshoot():
    n = LIF(1, tau_m_ms=20.0, dt_ms=1.0, threshold=1.0, reset_mode="subtract", dtype=torch.float64)
    spikes, st = drive(n, [1.7])
    assert spikes == [[1]] and st.v.item() == pytest.approx(0.7)


@pytest.mark.parametrize("k", [0, 1, 2, 5])
def test_refractory_blocks_exactly_k_steps(k):
    n = LIF(1, tau_m_ms=20.0, dt_ms=1.0, refractory_steps=k, dtype=torch.float64)
    spikes, _ = drive(n, [5.0] * 13)
    assert [s[0] for s in spikes] == [1 if t % (k + 1) == 0 else 0 for t in range(13)]


def test_refractory_clamps_voltage_to_reset():
    n = LIF(1, tau_m_ms=20.0, dt_ms=1.0, refractory_steps=2, v_reset=0.0, dtype=torch.float64)
    _, st = drive(n, [5.0, 0.3])
    assert st.v.item() == 0.0 and st.ref.item() == 1


def test_legacy_refractory_off_by_one_flag():
    n = LIF(1, tau_m_ms=20.0, dt_ms=1.0, refractory_steps=2, refractory_off_by_one=True, dtype=torch.float64)
    spikes, _ = drive(n, [5.0] * 6)
    assert [s[0] for s in spikes] == [1, 0, 1, 0, 1, 0]   # k=2 -> 1 blocked step (legacy)


def test_low_pass_integration_mode():
    n = LIF(1, tau_m_ms=5.0, dt_ms=1.0, current_integration="low_pass", dtype=torch.float64)
    _, st = drive(n, [0.4])
    a = math.exp(-1 / 5)
    assert st.v.item() == pytest.approx((1 - a) * 0.4)


def test_adaptation_increment_and_decay():
    n = AdaptiveLIF(1, tau_m_ms=20.0, dt_ms=1.0, tau_w_ms=300.0, b=0.2, refractory_steps=1, dtype=torch.float64)
    spikes, st = drive(n, [5.0])
    assert spikes == [[1]] and st.w.item() == pytest.approx(0.2)
    spikes, st = drive(n, [0.0], st)
    assert st.w.item() == pytest.approx(0.2 * math.exp(-1 / 300))


def test_adaptation_current_is_subtracted_next_step():
    n = AdaptiveLIF(1, tau_m_ms=1e9, dt_ms=1.0, tau_w_ms=1e9, b=0.3, refractory_steps=0, dtype=torch.float64)
    _, st = drive(n, [1.0])                 # spike, w = 0.3, v reset 0
    _, st = drive(n, [0.0], st)             # v = 0 + 0 - 0.3
    assert st.v.item() == pytest.approx(-0.3)


def test_non_adaptive_neurons_have_zero_w():
    n = AdaptiveLIF(2, tau_m_ms=5.0, dt_ms=1.0, tau_w_ms=250.0, b=torch.tensor([0.2, 0.0]), dtype=torch.float64)
    _, st = drive(n, [5.0] * 3)
    assert st.w[0, 1].item() == 0.0 and st.w[0, 0].item() > 0


def test_kick_is_voltage_increment_after_leak():
    n = LIF(1, tau_m_ms=20.0, dt_ms=1.0, dtype=torch.float64)
    st = n.init_state(1)
    st.v[:] = 0.2
    kick = torch.full((1, 1), 0.3, dtype=torch.float64)
    n.step(st, torch.zeros(1, 1, dtype=torch.float64), kick=kick)
    assert st.v.item() == pytest.approx(0.2 * math.exp(-1 / 20) + 0.3)
