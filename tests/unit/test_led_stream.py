"""Exercise the live model/LED boundary with deterministic spike trains and ACKs."""
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import torch

from ripples.reservoir import model_frames
from ripples.spikes import PIXELS, recorded_frames, send_frames
from ripples.hardware.transport import VALID_MASK


def test_live_windows_match_replay_and_acknowledged_blackout(tmp_path, monkeypatch):
    class Model:
        cfg = SimpleNamespace(dt_ms=1.)
        def reset_state(self):
            return iter(range(-1000, 6))
        def step(self, state):
            ids = {0: [0, 8459], 1: [0], 4: [1]}.get(next(state), [])
            spikes = torch.zeros(1, 8460, dtype=torch.bool)
            spikes[0, ids] = True
            return spikes

    events = []
    frames = list(model_frames(Model(), 2, 3, events))
    path = tmp_path / "spikes.npz"
    np.savez(path, steps=[0, 0, 1, 4], neuron_ids=[0, 8459, 0, 1],
             n_neurons=8460, n_steps=6, dt_ms=1)
    np.testing.assert_array_equal(frames, list(recorded_frames(path, 2)))
    assert [t for t, _ in events] == [0, 1, 4]
    assert np.all(frames[0][~VALID_MASK] == 0)
    assert np.array_equal(np.flatnonzero(frames[0][..., 2]), PIXELS[[0, -1]])
    assert not frames[1].any()

    class Link:
        serial = SimpleNamespace(is_open=True)
        def __init__(self):
            self.inflight = []
            self.displayed = []
        def submit(self, frame):
            assert len(self.inflight) < 2
            self.inflight.append(frame)
        def wait_next(self):
            self.displayed.append(self.inflight.pop(0))
        def send(self, frame):
            assert not self.inflight
            self.displayed.append(frame)

    link = Link()
    monkeypatch.setattr("ripples.spikes.time.sleep", lambda _: None)
    assert send_frames(link, frames) == 3
    np.testing.assert_array_equal(link.displayed[:-1], frames)
    assert not link.displayed[-1].any()
