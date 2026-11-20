from datetime import datetime, timedelta, timezone
import json

import numpy as np
import pytest

from ripples.paths import ROOT
from ripples.schedule import ExhibitionSchedule, StateController, KST
from ripples.normalization import normalize_features


def schedule():
    return ExhibitionSchedule(json.loads((ROOT/"configs/schedule.m30.json").read_text()))


@pytest.mark.parametrize("clock,expected,next_clock", [
    ("10:59:59", "sleep", "11:00:00"),
    ("11:00:00", "activate", "11:20:00"),
    ("11:19:59", "activate", "11:20:00"),
    ("11:20:00", "sleep", "11:30:00"),
    ("11:29:59", "sleep", "11:30:00"),
    ("11:30:00", "activate", "11:50:00"),
    ("11:50:00", "sleep", "12:00:00"),
    ("23:59:59", "sleep", "00:00:00"),
])
def test_wall_boundaries(clock, expected, next_clock):
    now = datetime.fromisoformat("2026-11-20T"+clock+"+09:00")
    phase = schedule().at(now)
    assert phase["state"] == expected
    assert datetime.fromisoformat(phase["next_transition"]).strftime("%H:%M:%S") == next_clock
    assert 0 < phase["remaining_seconds"] <= (1200 if expected == "activate" else 600)
    assert schedule().at(now.astimezone(timezone.utc)) == phase


def test_restart_jump_and_no_day_drift():
    base = datetime(2026, 11, 20, 11, 25, tzinfo=KST)
    # No remembered countdown: restart, clock correction, next day all resolve directly.
    s = schedule()
    assert s.at(base)["state"] == "sleep"
    assert s.at(base-timedelta(minutes=10))["state"] == "activate"
    for days in (1, 2, 31, 365):
        assert schedule().at(base+timedelta(days=days))["remaining_seconds"] == 300
    with pytest.raises(ValueError):
        s.at(base.replace(tzinfo=None))


def test_scheduled_ignores_old_manual_and_manual_is_explicit(tmp_path):
    path = tmp_path/"state.txt"
    path.write_text("activate")
    now = datetime(2026, 11, 20, 11, 25, tzinfo=KST)
    assert StateController(schedule(), state_file=path).at(now)["state"] == "sleep"
    manual = StateController(schedule(), mode="manual", state_file=path)
    assert manual.at(now)["state"] == "activate"
    path.write_text("sleep")
    assert manual.at(now)["state"] == "sleep"
    path.write_text("partial write")
    assert manual.at(now)["state"] == "sleep" and manual.at(now)["state_file_error"]
    with pytest.raises(ValueError):
        StateController(schedule(), initial_state="sleep")


@pytest.mark.parametrize("role,f", [
    ("hyeol", {"depth_0": 1500}), ("hyeol", {"depth_0": 0}),
    ("hyeol", {"depth_0": 2, "coverage_0": 0}),
    ("hyeol", {"ceiling_0": 60}), ("maek", {"mic_level_0": -45}),
    ("maek", {"mic_level_0": True}), ("maek", {"band_low": float("nan")}),
    ("maek", {"depth_0": 1}), ("hyeol", {"coverage_0": 1.1}),
])
def test_normalization_rejects_wrong_units_or_invalid_values(role, f):
    with pytest.raises(ValueError):
        normalize_features(role, f)


def test_physical_features_keep_scale_and_missing_is_not_zero():
    assert normalize_features("hyeol", {"depth_0": np.float64(1.5), "coverage_1": 0}) == {
        "depth_0": 1.5, "coverage_1": 0.0}
    assert normalize_features("maek", {"band_mid": .002, "mic_level_0": 0}) == {
        "band_mid": .002, "mic_level_0": 0.0}


def test_live_keeps_receiving_and_accumulating_across_boundaries(tmp_path, monkeypatch):
    """Run the actual live loop, receiver event path and SQLite worker; no network/USB."""
    import sys
    import ripples.live as live
    from ripples.hardware.transport import VALID_MASK
    from ripples.memory import SpatialMemory
    from ripples.realtime import LiveMemory, LiveSensors

    clocks = [datetime(2026, 11, 20, h, m, s, tzinfo=KST) for h, m, s in
              ((11, 19, 59), (11, 20, 0), (11, 29, 59), (11, 30, 0))]
    current, submitted, rendered, starts, closes = [clocks[0]], [], [], [], []
    original_at, original_submit = StateController.at, LiveMemory.submit
    original_render, original_close = live.RipplesVisual.render, LiveMemory.close
    monkeypatch.setattr(StateController, "at", lambda self, now=None: original_at(self, current[0]))

    def submit(self, event):
        submitted.append((schedule().at(current[0])["state"], event["features"]))
        original_submit(self, event)

    def close(self):
        closes.append(self)
        original_close(self)

    def render(self, raw, count, **kwargs):
        rendered.append((self.state, len(kwargs["events"])))
        return original_render(self, raw, count, **kwargs)

    def start(self, *args, **kwargs):
        starts.append(self)
        nodes = {k: {"state": "OK", "n": 3, "median_dbm": -70., "age_ms": 0}
                 for k in self.config["anchors"]}
        self.positions.update({"receivers": {"ripples-tof": {"received_ago_ms": 0, "nodes": nodes}}})
        self.emit("hyeol", "tof", {"depth_0": 1., "coverage_0": 1.})

    def source():
        raw = np.zeros((*VALID_MASK.shape, 3), np.uint8)
        yield raw, 0  # warmup frame, before receiver initialization
        for i, now in enumerate(clocks[1:], 1):
            current[0] = now
            starts[0].emit("hyeol", "tof", {"depth_0": 1.+i*.5, "coverage_0": 1.})
            yield raw, 0

    monkeypatch.setattr(LiveMemory, "submit", submit)
    monkeypatch.setattr(LiveMemory, "close", close)
    monkeypatch.setattr(LiveSensors, "start_center", start)
    monkeypatch.setattr(live.RipplesVisual, "render", render)
    monkeypatch.setattr(live, "recorded_spikes", source)
    report, db = tmp_path/"status.json", tmp_path/"memory.sqlite3"
    monkeypatch.setattr(sys, "argv", ["ripples.live", "--reservoir", "recorded", "--seconds", "0",
                                     "--accumulate", "--db", str(db), "--report", str(report)])
    live.main()
    assert len(starts) == len(closes) == 1
    assert [s for s, _ in submitted] == ["activate", "sleep", "sleep", "activate"]
    assert rendered == [(s, 1) for s, _ in submitted]
    status = json.loads(report.read_text())
    assert status["display_state"]["state"] == "activate" and status["accumulation_enabled"]
    assert status["frames"] == 4 and status["usb_requested"] is False
    memory = SpatialMemory(db, namespace="estimated")
    try:
        assert memory.snapshot()  # actual SQLite committed, no reset at state changes
    finally:
        memory.close()
