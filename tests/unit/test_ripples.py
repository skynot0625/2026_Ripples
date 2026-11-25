"""Verify localization, sensor validity, durable means and independent display states."""
import io
import json
from pathlib import Path
import struct
import sys
import time
import wave

import numpy as np
import pytest

from ripples.hardware.transport import VALID_MASK
from ripples.sensors import circular_beacons, estimate_position, depth_features, packets, wav_features
from ripples.memory import SpatialMemory
from ripples.visualization import RipplesVisual
from ripples.waves import SpikeWaves


def event(t, value, feature="ceiling_0", xy=(2, 1)):
    return {"t": t, "xy": xy, "source": "hyeol", "features": {feature: value}}


def test_beacon_inverse_and_bad_geometry():
    for t in np.linspace(0, 60, 61):
        rssi, estimate, truth = circular_beacons(t, noise_db=0)
        np.testing.assert_allclose(estimate, truth, atol=1e-5)
        assert (rssi < 0).all()
    assert estimate_position([127, -70, np.nan, -75]) is None
    assert estimate_position([-70]*4, [[0, 0], [1, 0], [2, 0], [3, 0]]) is None


def test_low_confidence_depth_and_rotation():
    d = np.full((6, 6), 3500, dtype="<u2")
    c = np.zeros_like(d)
    d[:2, :2], c[:2, :2] = 1000, 80
    h = dict(width=6, height=6, depth_dtype="<u2", conf_dtype="<u2")
    f = depth_features(h, d.tobytes()+c.tobytes(), rotate=180)
    assert f["depth_8"] == 1 and f["coverage_8"] == 1
    assert "depth_0" not in f and f["coverage_0"] == 0
    with pytest.raises(ValueError):
        depth_features(h, d.tobytes())


def test_packet_rejects_truncation_and_oversized_payload():
    h = json.dumps({"type": "hello"}).encode()
    assert list(packets(io.BytesIO(struct.pack("<II", len(h), 0)+h)))[0][0]["type"] == "hello"
    for bad in (b"123", struct.pack("<II", 1, 1 << 26), struct.pack("<II", 20, 0)+b"{}"):
        with pytest.raises(ValueError):
            list(packets(io.BytesIO(bad)))


def test_audio_power_is_linear_and_three_channels_stay_separate(tmp_path):
    t = np.arange(44100)/44100.
    pcm = np.column_stack((.1*np.sin(2*np.pi*440*t), .2*np.sin(2*np.pi*880*t), np.zeros_like(t)))
    path = tmp_path/"audio.wav"
    with wave.open(str(path), "wb") as w:
        w.setparams((3, 2, 44100, 0, "NONE", "not compressed"))
        w.writeframes((pcm*32767).astype("<i2").tobytes())
    f, duration = wav_features(path)
    assert duration == 1
    assert f["power_1"]/f["power_0"] == pytest.approx(4, rel=.002)
    assert f["centroid_0"] == pytest.approx(440, abs=1)
    assert f["centroid_1"] == pytest.approx(880, abs=1)
    assert f["power_2"] == 0 and "centroid_2" not in f


def test_time_balanced_durable_mean_dedup_and_atomic_rollback(tmp_path):
    path = tmp_path/"space.sqlite3"
    db = SpatialMemory(path)
    epoch = int(time.time()//86400)*86400
    samples = [event(i/100, 1.) for i in range(50)] + [event(1.1, 3.)]
    assert db.import_recording("one", samples, epoch=epoch)
    assert not db.import_recording("one", samples, epoch=epoch)
    row = db.snapshot()[0]
    assert row["features"]["ceiling_0"] == 2.  # not weighted 50:1
    assert row["counts"]["ceiling_0"] == 2
    with pytest.raises(ValueError):
        db.import_recording("broken", [event(2, 7), event(3, float("nan"))], epoch=epoch)
    assert db.snapshot()[0] == row
    db.close()
    db = SpatialMemory(path)
    assert db.snapshot()[0] == row
    db.close()
    with pytest.raises(ValueError, match="namespace"):
        SpatialMemory(path, namespace="measured")


def test_daily_retention_does_not_erase_lifetime_and_restarts_skip_audio(tmp_path):
    db = SpatialMemory(tmp_path/"m.db", retain_days=2)
    old = int(time.time()//86400-5)*86400
    db.import_recording("old", [event(0, 1)], epoch=old)
    db.import_recording("session", [event(0, 3)], sequence=0)
    db.import_recording("session", [event(1, 5)], sequence=44100)
    assert not db.import_recording("session", [event(0, 100)], sequence=0)
    assert db.snapshot()[0]["features"]["ceiling_0"] == 3
    assert db.db.execute("SELECT count(*) FROM moments WHERE day=?", (old//86400,)).fetchone()[0] == 0
    assert db.db.execute("SELECT count(*) FROM imports").fetchone()[0] == 2
    db.close()


def test_trails_expire_in_seconds_and_sleep_retains_wave_only_without_memory():
    raw = np.zeros((*VALID_MASK.shape, 3), np.uint8)
    a, b = RipplesVisual(), RipplesVisual()
    observation = event(0, 2.)
    first = a.render(raw, 0, events=[observation], dt=.1)
    b.render(raw, 0, events=[observation], dt=.125)
    assert first.any() and not first[~VALID_MASK].any()
    for _ in range(10):
        a.render(raw, 0, dt=.1)
    for _ in range(8):
        b.render(raw, 0, dt=.125)
    np.testing.assert_allclose(a.trail, b.trail, atol=1e-4)
    for _ in range(80):
        result = a.render(raw, 0, dt=.125)
    assert not result.any()
    a.set_state("sleep")
    assert not a.render(raw, 0).any()
    a.set_memory([{**observation, "counts": {"ceiling_0": 100}}])
    assert a.render(raw, 0).any()
    assert not a.render(raw, 0)[~VALID_MASK].any()


def test_audio_gap_cannot_discard_late_chunk(tmp_path):
    db = SpatialMemory(tmp_path/"m.db")
    assert db.import_recording("mic-session", [event(0, 1)], sequence=0, end_sequence=44100)
    with pytest.raises(ValueError, match="gap"):
        db.import_recording("mic-session", [event(2, 5)], sequence=88200, end_sequence=132300)
    assert db.import_recording("mic-session", [event(1, 3)], sequence=44100, end_sequence=88200)
    assert db.import_recording("mic-session", [event(2, 5)], sequence=88200, end_sequence=132300)
    assert db.snapshot()[0]["features"]["ceiling_0"] == 3
    db.close()


@pytest.mark.parametrize("observation", [
    {"source": "hyeol", "xy": (2, 1), "features": {"depth_4": 1.2, "coverage_4": .8, "ceiling_0": 2.1}},
    {"source": "maek", "xy": (-2, 1), "features": {"band_low": .01, "band_mid": .002, "band_high": .0001}},
])
def test_same_values_same_spatial_encoding_with_softer_sleep(observation):
    raw = np.zeros((*VALID_MASK.shape, 3), np.uint8)
    # Compare the shared spatial encoding before the nonlinear output budget.
    # Active sensory gain is 1.15; Sleep memory gain is 0.72.
    active = RipplesVisual("activate", output_limits=False)
    sleep = RipplesVisual("sleep", output_limits=False)
    sleep.set_memory([{**observation, "counts": {key: 10000 for key in observation["features"]}}])
    a = active.render(raw, 0, events=[observation])
    b = sleep.render(raw, 0)
    assert a.any()
    np.testing.assert_allclose(b, a.astype(float)*(.72/1.15), atol=1.)
    assert np.all(b <= a)
    # Incoming moving measurements cannot draw trails over Sleep's fixed mean.
    for _ in range(10):
        np.testing.assert_array_equal(sleep.render(raw, 0, events=[event(0, 3, xy=(-3, -2))]), b)
    sleep.set_memory([{**observation, "counts": {key: 1 for key in observation["features"]}}])
    np.testing.assert_array_equal(sleep.render(raw, 0), b)


def test_microphone_bands_and_no_phase_cancellation(tmp_path):
    t = np.arange(44100)/44100.
    signal = .15*np.sin(2*np.pi*100*t)
    pcm = np.column_stack((signal, -signal, signal))
    path = tmp_path/"phase.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((3, 2, 44100, 0, "NONE", "not compressed"))
        wav.writeframes((pcm*32767).astype("<i2").tobytes())
    features, _ = wav_features(path)
    assert features["band_low"] == pytest.approx(.15**2/2, rel=.001)
    assert features["band_mid"] < 1e-8 and features["band_high"] < 1e-8
    assert sum(features[f"band_{k}"] for k in ("low", "mid", "high")) == pytest.approx(
        np.mean([features[f"power_{i}"] for i in range(3)]), rel=1e-6)


def test_additive_feature_schema_migration_preserves_statistics(tmp_path):
    path = tmp_path/"memory.db"
    db = SpatialMemory(path)
    db.import_recording("history", [event(0, 2.)])
    before = db.snapshot()
    schema = json.loads(db.db.execute("SELECT value FROM settings WHERE key='schema'").fetchone()[0])
    schema[4] = [f for f in schema[4] if not f.startswith("band_")]
    with db.db:
        db.db.execute("UPDATE settings SET value=? WHERE key='schema'", (json.dumps(schema),))
    db.close()
    db = SpatialMemory(path)
    assert db.snapshot() == before
    assert not db.import_recording("history", [event(0, 2.)])
    db.close()


def test_std_is_sample_std_of_bin_means(tmp_path):
    db = SpatialMemory(tmp_path/"std.db")
    epoch = int(time.time())
    db.import_recording("stats", [event(.1, 1), event(1.1, 3)], epoch=epoch)
    assert db.snapshot()[0]["std"]["ceiling_0"] == pytest.approx(np.sqrt(2))
    db.close()


def test_std_shimmer_is_local_and_does_not_modify_means():
    raw = np.zeros((*VALID_MASK.shape, 3), np.uint8)
    observation = {**event(0, 2.), "counts": {"ceiling_0": 20}, "std": {"ceiling_0": .4}}
    a = RipplesVisual("sleep", seed=5)
    b = RipplesVisual("sleep", dream_strength=0., seed=5)
    for v in (a, b):
        v.set_memory([observation])
    before = a.memory.copy()
    frames = [a.render(raw, 0, dt=.1) for _ in range(20)]
    fixed = [b.render(raw, 0, dt=.1) for _ in range(3)]
    assert any(not np.array_equal(frames[0], f) for f in frames[1:])
    np.testing.assert_array_equal(fixed[0], fixed[-1])
    np.testing.assert_array_equal(a.memory, before)
    assert all(not f[~VALID_MASK].any() for f in frames)
    empty = RipplesVisual("sleep")
    assert not empty.render(raw, 0).any()


def test_activity_never_invents_radial_firing_and_slow_wave_requires_burst():
    raw = np.zeros((*VALID_MASK.shape, 3), np.uint8)
    pixel = tuple(np.argwhere(VALID_MASK)[0])
    raw[pixel] = (0, 160, 255)
    direct = RipplesVisual("sleep", criticality="activity")
    first = direct.render(raw, 1, dt=.1)
    assert np.count_nonzero(first.any(axis=-1)) == 1
    tail = direct.render(np.zeros_like(raw), 0, dt=.1)
    assert np.count_nonzero(tail.any(axis=-1)) == 1
    assert tail[pixel][2] < first[pixel][2]
    fast, slow = SpikeWaves(), SpikeWaves(time_scale=.3)
    fast.render(raw, 1)
    slow.render(raw, 1)
    assert not fast.fronts and not slow.fronts
    for v in (fast, slow):
        v.render(raw, 200, dt=.1)
        for _ in range(8):
            v.render(np.zeros_like(raw), 0, dt=.1)
    assert slow.fronts[0]["age"] == pytest.approx(fast.fronts[0]["age"]*.3)


def test_sleep_memory_is_soft_and_single_observation_does_not_shimmer():
    raw = np.zeros((*VALID_MASK.shape,3),np.uint8)
    observation={**event(0,2.), "counts":{"ceiling_0":1}, "std":{"ceiling_0":.4}}
    a=RipplesVisual("sleep",seed=7)
    a.set_memory([observation])
    assert not a.memory_spread.any()
    first=a.render(raw,0,dt=.1)
    for _ in range(20): last=a.render(raw,0,dt=.1)
    np.testing.assert_array_equal(first,last)
    # Mean storage is untouched; softness only affects the display.
    assert a.memory.max() > first.max() > 0


def test_sleep_std_survives_rgb565_and_board_brightness():
    from ripples.hardware.rgb565 import InstallationEncoder
    raw=np.zeros((*VALID_MASK.shape,3),np.uint8)
    visual=RipplesVisual("sleep")
    visual.set_memory([{"source":"hyeol", "xy":[2,1], "features":{"ceiling_0":2.},
                        "counts":{"ceiling_0":20}, "std":{"ceiling_0":.4}}])
    encoder=InstallationEncoder()
    frames=[visual.render(raw,0,dt=.1) for _ in range(120)]
    physical=np.array([encoder.preview_frame(f)[encoder.mask] for f in frames])
    assert physical.astype(float).std(axis=0).max() > .2
    assert physical.max() <= 12
    assert all(encoder.prepare(f)[encoder.mask].mean() <= 18 for f in frames)
