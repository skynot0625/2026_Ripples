"""Spec tests: kick eligibility/logging, replayable avalanche segmentation, E/I ratios."""

import numpy as np
import pytest

from snnrc.config import KickConfig, SegmentationConfig
from snnrc.criticality.activity import activity_metrics
from snnrc.criticality.segmentation import AvalancheTracker, segment_avalanches
from snnrc.reservoir.kick import KickController


# ------------------------------------------------------------------ kick eligibility
def ctrl(**kw):
    cfg = KickConfig(quiet_before_kick_ms=3.0, cooldown_ms=5.0, **kw)
    return KickController(cfg, dt_ms=1.0, target_ids=np.arange(4), rng=np.random.default_rng(0))


def test_kick_requires_quiet_window():
    c = ctrl()
    elig = []
    for t in range(5):
        elig.append(c.eligible(t, avalanche_active=False, pending=0))
        c.observe(t, quiet_activity=0)
    assert elig == [False, False, False, True, True]


def test_activity_resets_quiet_counter():
    c = ctrl()
    for t, a in enumerate([0, 0, 0, 1, 0, 0]):
        c.observe(t, quiet_activity=a)
    assert not c.eligible(6, False, 0)
    c.observe(6, 0)
    assert c.eligible(7, False, 0)


def test_no_kick_while_avalanche_active_or_pending():
    c = ctrl()
    for t in range(4):
        c.observe(t, 0)
    assert not c.eligible(4, avalanche_active=True, pending=0)
    assert not c.eligible(4, avalanche_active=False, pending=3)
    assert c.eligible(4, avalanche_active=False, pending=0)


def test_cooldown_after_attempt():
    c = ctrl()
    for t in range(4):
        c.observe(t, 0)
    rec = c.attempt(4, refractory=np.zeros(4, bool), state_before={"v": 0.0})
    assert rec["success"]
    for t in range(4, 12):
        c.observe(t, 0)
    assert [c.eligible(t, False, 0) for t in range(5, 11)] == [False] * 4 + [True, True]


def test_failed_kick_on_refractory_target_is_logged():
    c = ctrl(on_refractory_target="log_failed")
    rec = c.attempt(10, refractory=np.ones(4, bool), state_before={"v": 0.0})
    assert rec["success"] is False and rec["failure_reason"] == "target_refractory"
    assert c.table()[0]["kick_index"] == 0 and len(c.table()) == 1


def test_retarget_avoids_refractory_neurons():
    c = ctrl(on_refractory_target="retarget")
    refr = np.array([True, True, False, True])
    rec = c.attempt(10, refractory=refr, state_before={"v": 0.0})
    assert rec["success"] and rec["target_neuron_id"] == 2


# ------------------------------------------------------------------ segmentation
def seg(a, size=None, pending=None, close=2, req=True, forced=()):
    a = np.asarray(a)
    size = a if size is None else np.asarray(size)
    pending = np.zeros_like(a) if pending is None else np.asarray(pending)
    cfg = SegmentationConfig("t", "E", "E", float(close), req)   # dt = 1 ms: close steps == ms
    return segment_avalanches(a, size, pending, cfg, 1.0, forced_close_steps=forced)


def test_avalanche_start_continuation_and_close():
    tab = seg([0, 1, 2, 0, 1, 0, 0, 0, 3, 0, 0])
    assert [(r["start_step"], r["end_step"], r["size"]) for r in tab] == [(1, 4, 4), (8, 8, 3)]
    assert all(r["closed_normally"] for r in tab)
    assert tab[0]["duration_steps"] == 4 and tab[0]["peak_per_step"] == 2


def test_silence_shorter_than_close_window_does_not_split():
    tab = seg([1, 0, 1, 0, 0, 1], close=2)
    assert [(r["start_step"], r["end_step"]) for r in tab] == [(0, 2), (5, 5)]


def test_pending_spikes_delay_closure():
    a =       [1, 0, 0, 0, 0, 1, 0, 0]
    pending = [1, 1, 1, 1, 1, 0, 0, 0]
    tab = seg(a, pending=pending, close=2)
    assert len(tab) == 1 and (tab[0]["start_step"], tab[0]["end_step"]) == (0, 5)
    tab_np = seg(a, pending=pending, close=2, req=False)
    assert len(tab_np) == 2


def test_open_avalanche_at_end_is_flagged():
    tab = seg([0, 1, 1])
    assert len(tab) == 1 and tab[0]["closed_normally"] is False


def test_forced_safety_closure():
    tab = seg([1, 1, 1, 1, 0, 0], forced=(1,))
    assert tab[0]["hit_safety_cutoff"] and (tab[0]["start_step"], tab[0]["end_step"]) == (0, 1)
    assert tab[1]["start_step"] == 2


def test_E_boundary_vs_total_boundary_and_sizes():
    nE = np.array([1, 0, 0, 0, 2, 0, 0, 0])
    nI = np.array([0, 3, 3, 3, 0, 1, 0, 0])
    tot = nE + nI
    zero = np.zeros_like(nE)
    E_seg_E = segment_avalanches(nE, nE, zero, SegmentationConfig("p", "E", "E", 2.0, True), 1.0)
    T_seg_E = segment_avalanches(tot, nE, zero, SegmentationConfig("a", "total", "E", 2.0, True), 1.0)
    T_seg_T = segment_avalanches(tot, tot, zero, SegmentationConfig("b", "total", "total", 2.0, True), 1.0)
    assert [r["size"] for r in E_seg_E] == [1, 2]
    assert [r["size"] for r in T_seg_E] == [3]
    assert [r["size"] for r in T_seg_T] == [13]


def test_online_tracker_equals_offline_replay():
    rng = np.random.default_rng(1)
    a = (rng.random(500) < 0.3) * rng.integers(1, 4, 500)
    pending = (rng.random(500) < 0.1).astype(int)
    cfg = SegmentationConfig("p", "E", "E", 3.0, True)
    tr = AvalancheTracker(cfg, 1.0)
    online = []
    for t in range(500):
        r = tr.update(t, int(a[t]), int(a[t]), int(pending[t]))
        if r is not None:
            online.append(r)
    online += tr.finish(499)
    assert online == segment_avalanches(a, a, pending, cfg, 1.0)


# ------------------------------------------------------------------ E/I ratios
def test_R_total_and_R_per_neuron():
    nE = np.array([4, 4, 0, 0]); nI = np.array([1, 1, 0, 0])
    m = activity_metrics(nE, nI, N_E=800, N_I=200, dt_ms=1.0)
    assert m["E_total_spikes"] == 8 and m["I_total_spikes"] == 2
    assert m["R_total"] == pytest.approx(4.0)
    assert m["R_per_neuron"] == pytest.approx((8 / 800) / (2 / 200)) == pytest.approx(1.0)
    assert m["E_rate_hz"] == pytest.approx(8 / 800 / 0.004)
    assert m["I_rate_hz"] == pytest.approx(2 / 200 / 0.004)
    assert m["E_population_spikes_per_step"] == pytest.approx(2.0)
    assert m["population_silent_fraction"] == pytest.approx(0.5)
    assert m["structural_N_E_over_N_I"] == 4.0


def test_zero_I_spikes_marked_not_epsilon():
    m = activity_metrics(np.array([3, 0]), np.array([0, 0]), 800, 200, 1.0)
    assert m["R_total"] is None and m["R_per_neuron"] is None
    assert m["ratio_status"] == "undefined_zero_I_spikes"
