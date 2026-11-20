"""Synthetic protocol fixtures only; no USB and no actual center mutations."""
import copy
import json
import socket
import struct
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from ripples.memory import SpatialMemory
from ripples.realtime import (LiveMemory, LiveSensors, Positions, TOF_HEADER, decode_monitor,
                              recv_packet, validate_config, StreamClock)
from ripples.sensors import depth_features, pcm_features
from ripples.visualization import RipplesVisual


@pytest.fixture
def config():
    p = Path(__file__).resolve().parents[2]/"configs/live.example.json"
    return json.loads(p.read_text())


def beacons(c, xy=(0, 0), age=0):
    nodes = {}
    for key, a in c["anchors"].items():
        distance = np.linalg.norm(np.asarray(a["xy_m"])-xy)
        nodes[key] = {"state": "OK", "n": 3, "age_ms": age,
                      "median_dbm": a["reference_dbm"]-10*c["exponent"]*np.log10(distance)}
    return {"receivers": {r: {"nodes": copy.deepcopy(nodes), "received_ago_ms": 0}
                           for r in c["receivers"].values()}}


def packet(h, b=b""):
    header = json.dumps(h).encode()
    return struct.pack("<II", len(header), len(b))+header+b


def wait_for(condition, seconds=4):
    until = time.monotonic()+seconds
    while time.monotonic() < until:
        if condition():
            return
        time.sleep(.01)
    assert condition()


def test_positions_are_never_invented(config):
    p = Positions(config)
    p.update(beacons(config), now=10)
    assert p.get("hyeol", now=10) is None
    config["calibrated"] = True
    p = Positions(config)
    p.update(beacons(config, (1, -2)), now=10)
    np.testing.assert_allclose(p.get("hyeol", now=10), [1, -2], atol=1e-5)
    assert p.get("hyeol", now=13.01) is None
    doc = beacons(config)
    for node in ("1", "2"):
        doc["receivers"]["ripples-tof"]["nodes"][node]["state"] = "CONFLICT"
    p.update(doc, now=11)
    assert p.get("hyeol", now=11) is None
    assert p.get("maek", now=11) is not None


def test_beacon_age_adds_transport_age_and_maps_physical_bounds(config):
    config["calibrated"] = True
    config["bounds_m"] = [[-10, 10], [-20, 20]]
    p = Positions(config)
    doc = beacons(config, (1, -2), age=1200)
    doc["receivers"]["ripples-tof"]["received_ago_ms"] = 1700
    p.update(doc, now=5)
    np.testing.assert_allclose(p.get("hyeol", now=5), [.5, -.5], atol=1e-5)
    assert p.get("hyeol", now=5.2) is None


def test_residual_gate_rejects_impossible_distances(config):
    config["calibrated"] = True
    p = Positions(config)
    doc = beacons(config)
    for body in doc["receivers"].values():
        for n in body["nodes"].values():
            n["median_dbm"] = -30
    p.update(doc)
    assert p.get("hyeol") is None


def test_fragmented_tcp_and_bounded_lengths():
    a, b = socket.socketpair()
    wire = packet({"type": "hello", "version": 1}, b"abc")
    def send():
        with a:
            for i in range(0, len(wire), 3):
                a.sendall(wire[i:i+3])
    t = threading.Thread(target=send)
    t.start()
    with b:
        assert recv_packet(b) == ({"type": "hello", "version": 1}, b"abc")
        with pytest.raises(ConnectionError):
            recv_packet(b)
    t.join()
    a, b = socket.socketpair()
    with a, b:
        a.sendall(struct.pack("<II", 10, 50_000_000))
        with pytest.raises(ValueError, match="size"):
            recv_packet(b)


def test_monitor_no_double_rotation_and_confidence_mask():
    depth = np.full((6, 6), 2000, "<u2")
    depth[:2, :2] = 1000
    depth[-2:, -2:] = 0
    wire = TOF_HEADER.pack(b"TOF3", 0, 0, 6, 6, 5, 1000., 0., 200, 4000, 1, 50, 100, 200, 60, 0, 100)
    key, f = decode_monitor(wire+depth.tobytes()+b"POSE optional trailer")
    assert key == (5, 1000.) and f["depth_0"] == 1
    assert "depth_8" not in f and f["coverage_8"] == 0
    with pytest.raises(ValueError):
        decode_monitor(wire)
    header = {"width": 6, "height": 6, "depth_dtype": "<u2", "conf_dtype": "<u2"}
    confidence = np.full_like(depth, 80)
    f = depth_features(header, depth.tobytes()+confidence.tobytes(), rotate=180)
    assert f["depth_8"] == 1 and "depth_0" not in f


def test_pcm_bands_and_channel_phase_do_not_cancel():
    t = np.arange(4410)/44100
    signal = (np.sin(2*np.pi*1000*t)*6000).astype("<i2")
    raw = np.column_stack((signal, -signal, signal)).astype("<i2").tobytes()
    f = pcm_features(raw)
    assert f["band_mid"] > .015
    assert f["band_mid"] > 100*f["band_low"]
    assert f["centroid_0"] == pytest.approx(1000, abs=1)


def test_new_live_levels_are_not_frequency_bands():
    v = np.zeros((32, 32, 24, 3), np.float32)
    RipplesVisual.stamp(v, {"source": "maek", "xy": [0, 0], "features": {"mic_level_0": .7}})
    assert v[:, :, 2].any() and v[:, :, 7].any() and not v[:, :, 12:].any()


def test_latest_visual_and_stale_position(config):
    config["calibrated"] = True
    s = LiveSensors(config)
    s.positions.update(beacons(config))
    for i in range(100):
        s.emit("hyeol", "tof", {"depth_0": 1+i*.01, "coverage_0": 1})
    events = s.drain()
    assert len(events) == 1 and events[0]["features"]["depth_0"] == 1.99
    assert s.drain() == [] and s.counts["visual_coalesced"] == 99
    s.positions.update({})
    s.emit("hyeol", "tof", {"depth_0": 1})
    assert s.drain() == [] and s.counts["unpositioned"] == 1


def test_raw_receiver_preprocesses_and_reconnects(config):
    config["calibrated"] = True
    s = LiveSensors(config)
    s.positions.update(beacons(config))
    port = s.start_collector("127.0.0.1", 0)
    h = {"type": "tof", "seq": 1, "t_wall_ns": time.time_ns(), "width": 6, "height": 6,
         "depth_dtype": "<u2", "conf_dtype": "<u2"}
    raw = np.full((6, 6), 1500, "<u2").tobytes()+np.full((6, 6), 80, "<u2").tobytes()
    try:
        for n in (1, 2):
            with socket.create_connection(("127.0.0.1", port)) as c:
                c.sendall(packet({"type": "hello", "version": 1}))
                c.sendall(packet(h, raw))
                c.sendall(packet(h, raw))
                wait_for(lambda: s.counts["raw_tof_features"] >= n)
                assert s.drain()[0]["features"]["depth_4"] == 1.5
        assert s.counts["duplicate_raw"] >= 1
    finally:
        s.close()


def test_live_memory_seconds_variance_restart_and_calibration(tmp_path, config):
    config["calibrated"] = True
    path = tmp_path/"measured.db"
    second = int(time.time())-20
    m = LiveMemory(path, config)
    for value in (1., 3.):
        m.submit({"source": "hyeol", "xy": [0, 0], "features": {"depth_0": value}, "t": second})
    m.submit({"source": "hyeol", "xy": [0, 0], "features": {"depth_0": 4.}, "t": second+1})
    m.close()
    assert m.error is None and m.saved_seconds == 2
    db = SpatialMemory(path, namespace="measured")
    entry = db.snapshot()[0]
    assert entry["features"]["depth_0"] == 3.
    assert entry["counts"]["depth_0"] == 2
    assert entry["std"]["depth_0"] == pytest.approx(np.sqrt(2))
    assert db.db.execute("SELECT count(*) FROM imports").fetchone()[0] == 1
    db.close()
    m = LiveMemory(path, config)
    m.submit({"source": "hyeol", "xy": [0, 0], "features": {"depth_0": 99.}, "t": second+1})
    m.close()
    assert m.saved_seconds == 0
    config["exponent"] = 3.9
    with pytest.raises(RuntimeError, match="calibration"):
        LiveMemory(path, config)


def test_invalid_config(config):
    config["bounds_m"] = [[0, 0], [0, 10]]
    with pytest.raises(ValueError):
        validate_config(config)


def test_relative_position_uses_confirmed_five_nodes_without_rf_reference(config):
    from ripples.site import site_config
    simulator = {"layout": {"beacons": {
        "1": {"x": 1000, "y": 1000, "z": 0}, "2": {"x": 9000, "y": 1000, "z": 0},
        "3": {"x": 9000, "y": 9000, "z": 0}, "4": {"x": 1000, "y": 9000, "z": 0},
        "5": {"x": 5000, "y": 5000, "z": 0}}}}
    venue = {"id": "m30", "units": "mm", "bounds": {"x0": 0, "x1": 10000, "y0": 0, "y1": 10000}}
    c = site_config(config, simulator, venue)
    assert not c["calibrated"] and c["coordinates_confirmed"]
    assert c["anchors"]["2"]["xy_m"] == [9, 1]
    nodes = {k: {"state": "OK", "n": 3, "age_ms": 0, "median_dbm": -60} for k in c["anchors"]}
    doc = {"receivers": {"ripples-tof": {"received_ago_ms": 0, "nodes": nodes}}}
    p = Positions(c)
    p.update(doc)
    np.testing.assert_allclose(p.get("hyeol"), [0, 0], atol=1e-8)
    nodes["1"]["median_dbm"] = -40
    p.update(doc)
    first = p.get("hyeol")
    assert first[0] < 0 and first[1] < 0
    # Adding a shared RSSI offset must not change this relative position.
    for n in nodes.values():
        n["median_dbm"] -= 20
    p.update(doc)
    np.testing.assert_allclose(p.get("hyeol"), first)
    assert p.report()["hyeol"]["approximate"] and p.report()["hyeol"]["nodes_used"] == 5
    nodes["1"]["state"] = "CONFLICT"
    p.update(doc)
    assert p.report()["hyeol"]["nodes_used"] == 4
    nodes["1"].update(age_ms=None, median_dbm=None, n=None)
    p.update(doc)
    assert p.report()["hyeol"]["nodes_used"] == 4
    c["coordinates_confirmed"] = False
    with pytest.raises(ValueError, match="confirmed"):
        validate_config(c)


def test_site_export_refuses_wrong_units_and_outside_nodes(config):
    from ripples.site import site_config
    with pytest.raises(ValueError, match="millimetres"):
        site_config(config, {}, {"id": "m30", "units": "m"})


def test_raw_source_clock_ignores_wall_skew_and_rejects_backlog():
    clock = StreamClock(.5)
    h = {"type": "tof", "t_mono_ns": 1_000_000_000, "t_wall_ns": 0}
    assert clock.accept(h, 100) == "source_monotonic"
    h.update(t_mono_ns=1_100_000_000, t_wall_ns=9999999999999999999)
    assert clock.accept(h, 100.1) == "source_monotonic"
    h["t_mono_ns"] = 1_200_000_000
    with pytest.raises(ValueError, match="backlog"):
        clock.accept(h, 101)
    assert StreamClock(.5).accept(h, 101) == "source_monotonic"
    assert StreamClock(.5).accept({"type": "audio"}, 102) == "arrival_only"


def test_estimated_memory_cannot_mix_with_calibrated_memory(tmp_path, config):
    config.update(position_mode="relative_rssi", coordinates_confirmed=True)
    path = tmp_path/"relative.db"
    m = LiveMemory(path, config)
    m.submit({"t": time.time(), "source": "hyeol", "xy": [0, 0], "features": {"ceiling_0": 2}})
    m.close()
    assert m.error is None and m.saved_seconds == 1
    with pytest.raises(ValueError, match="namespace"):
        SpatialMemory(path, namespace="measured")


def test_receive_and_live_do_not_open_memory_by_default(tmp_path, monkeypatch):
    import sys
    import ripples.live as live
    import ripples.receive as receive
    def forbidden(*args, **kwargs):
        raise AssertionError("default runs must not open spatial memory")
    monkeypatch.setattr(live, "LiveMemory", forbidden)
    monkeypatch.setattr(LiveSensors, "start_center", lambda *args, **kwargs: None)
    for module in (receive, live):
        report = tmp_path/(module.__name__+".json")
        args = [module.__name__, "--seconds", ".02", "--report", str(report)]
        if module is live:
            args += ["--reservoir", "recorded", "--state-file", str(tmp_path/"state.txt")]
        monkeypatch.setattr(sys, "argv", args)
        module.main()
        status = json.loads(report.read_text())
        assert status["memory_db"] is None and status["accumulation_enabled"] is False
    assert not list(tmp_path.glob("*.sqlite3*"))


def test_preview_read_snapshot_does_not_update_database(tmp_path):
    path = tmp_path/"demo.sqlite3"
    memory = SpatialMemory(path)
    memory.import_recording("test", [{"t": 0, "source": "hyeol", "xy": [0, 0], "features": {"ceiling_0": 2}}])
    memory.close()
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert SpatialMemory.read_snapshot(path)[0]["features"]["ceiling_0"] == 2
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("mode", ["range_fit", "relative_rssi"])
def test_center_http_two_instruments_memory_and_input_loss(tmp_path, config, mode):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    config["calibrated"] = True
    if mode == "relative_rssi":
        config.update(calibrated=False, coordinates_confirmed=True, position_mode=mode)
    state = {"seq": 0, "connected": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/status":
                doc = {"mock": False, "connected": True, "confidence": 30}
            elif self.path == "/inputs":
                state["seq"] += 1
                age = 0 if state["connected"] else 4
                doc = {"mock": False, "channels": {
                    "distance": {"source": "TOFSense (collector)", "seq": state["seq"],
                                 "connected": state["connected"], "age_s": age, "value": 2.4},
                    "maek_mic": {"source": "maek-pi:mic-001", "seq": state["seq"],
                                 "connected": state["connected"], "age_s": age, "value": [.2, .5, .8]}}}
            else:
                doc = beacons(config)
            data = json.dumps(doc).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    path = tmp_path/"http-fixture.db"
    memory = LiveMemory(path, config)
    sensors = LiveSensors(config, memory)
    try:
        sensors.start_center(f"http://127.0.0.1:{server.server_port}", tof=False)
        wait_for(lambda: sensors.counts["positioned"] >= 6)
        events = sensors.drain()
        assert {e["source"] for e in events} == {"hyeol", "maek"}
        assert not sensors.errors
        wait_for(lambda: memory.saved_seconds >= 1)
        state["connected"] = False
        time.sleep(.25)
        sensors.drain()
        time.sleep(.25)
        assert sensors.drain() == []
    finally:
        sensors.close()
        memory.close()
        server.shutdown()
        server.server_close()
        serving.join()
    assert memory.error is None
    db = SpatialMemory(path, namespace="estimated" if mode == "relative_rssi" else "measured")
    try:
        entries = {e["source"]: e for e in db.snapshot()}
        assert entries["hyeol"]["features"]["ceiling_0"] == pytest.approx(2.4)
        assert entries["maek"]["features"]["mic_level_1"] == .5
        assert "band_mid" not in entries["maek"]["features"]
    finally:
        db.close()
