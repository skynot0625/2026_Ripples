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
                              recv_packet, validate_config)
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
    assert v[:, :, 2].any() and not v[:, :, 4:].any()


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


def test_center_http_two_instruments_memory_and_input_loss(tmp_path, config):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    config["calibrated"] = True
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
    db = SpatialMemory(path, namespace="measured")
    try:
        entries = {e["source"]: e for e in db.snapshot()}
        assert entries["hyeol"]["features"]["ceiling_0"] == pytest.approx(2.4)
        assert entries["maek"]["features"]["mic_level_1"] == .5
        assert "band_mid" not in entries["maek"]["features"]
    finally:
        db.close()
