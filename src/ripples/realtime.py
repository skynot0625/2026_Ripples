"""Read-only center bridge and bounded tower-side sensor processing.

No USB, server start, synthetic position, or network access at import time.
"""
from collections import Counter
import hashlib
import json
import queue
import socket
import struct
import threading
import time
from urllib.request import urlopen

import numpy as np

from ripples.memory import SpatialMemory, cell_xy
from ripples.sensors import depth_features, depth_grid_features, estimate_position, pcm_features
from ripples.visualization import RipplesVisual

TOF_HEADER = struct.Struct("<4sBBHHIdfHHBBHHBBH")


def validate_config(config):
    c = dict(config)
    mode = c.get("position_mode", "range_fit")
    if mode not in ("range_fit", "relative_rssi"):
        raise ValueError("unknown position mode")
    if mode == "relative_rssi" and c.get("coordinates_confirmed") is not True:
        raise ValueError("relative RSSI requires confirmed beacon coordinates")
    anchors = c["anchors"]
    points = np.asarray([a["xy_m"] for a in anchors.values()], float)
    refs = np.asarray([a["reference_dbm"] for a in anchors.values()], float)
    bounds = np.asarray(c["bounds_m"], float)
    if (not 3 <= len(anchors) <= 32 or points.shape != (len(anchors), 2)
            or not np.isfinite(points).all() or np.linalg.matrix_rank(points-points.mean(0)) < 2
            or not np.isfinite(refs).all() or np.any(refs >= 0) or np.any(refs <= -127)
            or bounds.shape != (2, 2) or not np.isfinite(bounds).all()
            or np.any(bounds[:, 1] <= bounds[:, 0])):
        raise ValueError("invalid anchors/reference RSSI/bounds")
    for key in ("exponent", "stale_s", "max_residual_m", "max_sensor_age_s"):
        if not np.isfinite(c[key]) or c[key] <= 0:
            raise ValueError(f"invalid {key}")
    if type(c["calibrated"]) is not bool or c["rotate"] not in (0, 90, 180, 270):
        raise ValueError("invalid calibrated/rotate")
    if not 0 <= c["confidence"] <= 65535 or not 0 <= c["monitor_confidence"] <= 65535:
        raise ValueError("invalid confidence threshold")
    if set(c["receivers"]) != {"hyeol", "maek"} or not all(isinstance(x, str) and x for x in c["receivers"].values()):
        raise ValueError("receiver names required")
    return c


def memory_namespace(config):
    return "estimated" if config.get("position_mode") == "relative_rssi" else "measured"


def position_enabled(config):
    return config.get("position_mode") == "relative_rssi" or config["calibrated"]


def calibration_id(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


class Positions:
    def __init__(self, config):
        self.config = validate_config(config)
        self.lock = threading.Lock()
        self.values = {}
        self.details = {}

    def update(self, doc, now=None):
        now = time.monotonic() if now is None else now
        c, values, details = self.config, {}, {}
        relative = c.get("position_mode") == "relative_rssi"
        if position_enabled(c):
            for role, receiver in c["receivers"].items():
                body = doc.get("receivers", {}).get(receiver, {})
                elapsed_ms = body.get("received_ago_ms")
                elapsed = elapsed_ms/1000. if isinstance(elapsed_ms, (int, float)) else float("inf")
                details[role] = {"reason": "stale_receiver", "mode": c.get("position_mode", "range_fit")}
                if not np.isfinite(elapsed) or not 0 <= elapsed < c["stale_s"]:
                    continue
                points, refs, rssi, ages = [], [], [], []
                for key, anchor in c["anchors"].items():
                    n = body.get("nodes", {}).get(key, {})
                    v = n.get("median_dbm")
                    age_ms = n.get("age_ms")
                    age = elapsed + age_ms/1000. if isinstance(age_ms, (int, float)) else float("inf")
                    samples = n.get("n")
                    if (n.get("state") == "OK" and isinstance(samples, (int, float)) and samples > 0
                            and (not anchor.get("hardware_id") or n.get("hardware_id") == anchor["hardware_id"])
                            and isinstance(v, (int, float)) and np.isfinite(v) and -127 < v < 0
                            and np.isfinite(age) and 0 <= age < c["stale_s"]):
                        points.append(anchor["xy_m"])
                        refs.append(anchor["reference_dbm"])
                        rssi.append(v)
                        ages.append(age)
                if len(points) < 3:
                    details[role].update(reason="insufficient_fresh_nodes", nodes_used=len(points))
                    continue
                if np.linalg.matrix_rank(np.asarray(points)-np.mean(points, axis=0)) < 2:
                    details[role]["reason"] = "collinear_nodes"
                    continue
                if relative:
                    # Relative d^-2 weights: d ratios use P proportional to d^-exponent.
                    # No assumed reference RSSI is converted to a claimed metre range.
                    weights = 10.**((np.asarray(rssi)-max(rssi))/(5*c["exponent"]))
                    xy = np.average(points, axis=0, weights=weights)
                    residual = None
                else:
                    xy = estimate_position(rssi, points, refs, c["exponent"])
                    distances = 10.**((np.asarray(refs)-rssi)/(10*c["exponent"]))
                    residual = float(np.sqrt(np.mean((np.linalg.norm(points-xy, axis=1)-distances)**2))) if xy is not None else None
                if xy is None:
                    continue
                bounds = np.asarray(c["bounds_m"])
                if residual is not None and residual > c["max_residual_m"]:
                    details[role].update(reason="range_residual", residual_m=residual)
                    continue
                if np.any(xy < bounds[:, 0]) or np.any(xy > bounds[:, 1]):
                    details[role]["reason"] = "outside_bounds"
                    continue
                # The renderer/memory use canonical [-5,5]; physical metres remain in config.
                mapped = (xy-bounds[:, 0])/(bounds[:, 1]-bounds[:, 0])*10-5
                values[role] = (mapped.tolist(), now+c["stale_s"]-max(ages))
                details[role].update(reason="ok", xy_m=xy.tolist(), nodes_used=len(points),
                                     residual_m=residual, approximate=relative)
        with self.lock:
            self.values = values
            self.details = details

    def get(self, role, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            value = self.values.get(role)
        return value[0] if value and now < value[1] else None

    def report(self):
        with self.lock:
            return {k: dict(v) for k, v in self.details.items()}


def decode_monitor(packet):
    """TOF3 monitor depth is already oriented and confidence-masked by the center.

    Trailers (POSE/EDGL) are intentionally ignored; they are not global calibrated XY.
    """
    if not isinstance(packet, bytes) or len(packet) < TOF_HEADER.size:
        raise ValueError("truncated TOF3 header")
    magic, mode, _, w, h, seq, capture_ms, *_ = TOF_HEADER.unpack_from(packet)
    end = TOF_HEADER.size+2*w*h
    if magic != b"TOF3" or mode != 0 or not 3 <= w <= 2048 or not 3 <= h <= 2048 or end > len(packet):
        raise ValueError("invalid monitor depth")
    depth = np.frombuffer(packet, "<u2", count=w*h, offset=TOF_HEADER.size).reshape(h, w)
    features = depth_grid_features(depth, (depth > 0) & (depth <= 4000))
    return (seq, capture_ms), features


def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size-len(data))
        if not chunk:
            raise ConnectionError("collector disconnected")
        data.extend(chunk)
    return bytes(data)


def recv_packet(sock):
    nh, nb = struct.unpack("<II", recv_exact(sock, 8))
    if not 0 < nh <= 65536 or nb > 2*1024*1024:
        raise ValueError("packet exceeds live size limit")
    h = json.loads(recv_exact(sock, nh))
    if not isinstance(h, dict):
        raise ValueError("header must be an object")
    return h, recv_exact(sock, nb)


class StreamClock:
    """Compare remote monotonic intervals, never Pi wall time against PC wall time.

    The first packet's one-way latency is unknowable without synchronization.
    Subsequent excess queueing over the smallest observed transit offset is bounded.
    """
    def __init__(self, max_lag):
        self.max_lag = max_lag
        self.offset = None
        self.last = {}

    def accept(self, header, now):
        if "t_mono_ns" not in header:
            return "arrival_only"  # legacy PCM extension; no capture-age claim
        stamp = header["t_mono_ns"]
        if type(stamp) is not int or stamp < 0:
            raise ValueError("invalid source monotonic timestamp")
        kind = header["type"]
        if stamp <= self.last.get(kind, -1):
            raise ValueError("source clock reversed; reconnect required")
        self.last[kind] = stamp
        offset = now-stamp/1e9
        self.offset = offset if self.offset is None else min(self.offset, offset)
        if offset-self.offset > self.max_lag:
            raise ValueError("source backlog exceeded; reconnect required")
        return "source_monotonic"


class LiveMemory:
    """One SQLite owner, complete 1 s bins, fixed import cursor, bounded ingress.

    Raw frames are not archived. Overload/write failures are observable, never ACKed
    as durable raw recordings. Only accepted features enter sufficient statistics.
    """
    def __init__(self, path, config, *, capacity=2048):
        self.path, self.config = path, config
        self.queue = queue.Queue(maxsize=capacity)
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.dropped = 0
        self.saved_seconds = 0
        self.snapshot = None
        self.thread = threading.Thread(target=self._run, name="spatial-memory", daemon=True)
        self.thread.start()
        if not self.ready.wait(10) or self.error:
            self.stop.set()
            raise RuntimeError(f"memory initialization failed: {self.error}")

    def submit(self, event):
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        db = None
        try:
            db = SpatialMemory(self.path, namespace=memory_namespace(self.config))
            ident = calibration_id(self.config)
            old = db.db.execute("SELECT value FROM settings WHERE key='live_calibration'").fetchone()
            if old and old[0] != ident:
                raise ValueError("live calibration changed; use a new DB")
            with db.db:
                db.db.execute("INSERT OR IGNORE INTO settings VALUES ('live_calibration',?)", (ident,))
            visual = RipplesVisual("sleep")

            def publish():
                visual.set_memory(db.snapshot())
                self.snapshot = (visual.memory.copy(), visual.memory_spread.copy())

            publish()
            self.ready.set()
            second, bucket = None, {}
            published = time.monotonic()

            def flush():
                if not bucket:
                    return
                events = []
                for (source, x, y), values in bucket.items():
                    events.append({"source": source, "xy": [(x+.5)/24*10-5, (y+.5)/24*10-5],
                                   "t": second, "features": {k: v[0]/v[1] for k, v in values.items()}})
                if db.import_recording("live:"+ident, events, epoch=0, sequence=second):
                    self.saved_seconds += 1
                else:
                    # A restart/clock rollback must not count the same second twice.
                    self.dropped += len(events)

            while not self.stop.is_set() or not self.queue.empty():
                try:
                    event = self.queue.get(timeout=.1)
                except queue.Empty:
                    if second is not None and time.time() >= second+1:
                        flush()
                        second, bucket = None, {}
                    event = None
                if event is not None:
                    tick = int(event["t"])
                    if second is not None and tick < second:
                        self.dropped += 1
                        continue
                    if tick != second:
                        flush()
                        second, bucket = tick, {}
                    cell = cell_xy(event["xy"])
                    if cell is not None:
                        group = bucket.setdefault((event["source"], *cell), {})
                        for feature, value in event["features"].items():
                            total, n = group.get(feature, (0., 0))
                            group[feature] = (total+value, n+1)
                if time.monotonic()-published >= 2:
                    publish()
                    published = time.monotonic()
            flush()
            publish()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.ready.set()
            if db is not None:
                db.close()

    def close(self):
        self.stop.set()
        self.thread.join(10)
        if self.thread.is_alive():
            raise RuntimeError("spatial memory did not stop")


class LiveSensors:
    def __init__(self, config, memory=None):
        self.config = validate_config(config)
        self.positions = Positions(config)
        self.memory = memory
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.latest = {}
        self.raw = {}
        self.counts = Counter()
        self.errors = {}
        self.threads = []
        self.center_valid = False
        self.seen = {}
        self.input_status = {}
        self.last_poll = None

    def emit(self, role, channel, features):
        self.counts[channel+"_features"] += 1
        xy = self.positions.get(role)
        if xy is None:
            self.counts["unpositioned"] += 1
            return
        if not features or not all(np.isfinite(x) for x in features.values()):
            raise ValueError("nonfinite/empty sensor features")
        event = {"source": role, "xy": xy, "features": features, "t": time.time()}
        with self.lock:
            if channel in self.latest:
                self.counts["visual_coalesced"] += 1
            self.latest[channel] = (time.monotonic(), event)
        if self.memory:
            self.memory.submit(event)
        self.counts["positioned"] += 1

    def drain(self):
        with self.lock:
            latest, self.latest = self.latest, {}
        now = time.monotonic()
        return [event for stamp, event in latest.values() if now-stamp < .5]

    def start_thread(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        self.threads.append(thread)
        thread.start()

    def start_center(self, url, *, tof=True, distance=True, mic=True):
        self.start_thread(self._poll_center, url.rstrip("/"), distance, mic)
        if tof:
            self.start_thread(self._monitor, url.rstrip("/"))

    def _poll_center(self, url, distance=True, mic=True):
        def get(path):
            with urlopen(url+path, timeout=1) as response:
                data = response.read(262145)
                if len(data) > 262144:
                    raise ValueError("center JSON too large")
                return json.loads(data)
        while not self.stop.is_set():
            begin = time.monotonic()
            try:
                status, inputs, beacons = get("/status"), get("/inputs"), get("/api/beacon")
                self.input_status = {name: {k: ch.get(k) for k in ("source", "connected", "age_s", "rate_hz", "seq", "error")}
                                     for name, ch in inputs.get("channels", {}).items()}
                self.last_poll = time.monotonic()
                self.center_valid = (status.get("mock") is False and status.get("connected") is True
                                     and status.get("confidence") == self.config["monitor_confidence"])
                if inputs.get("mock") is not False:
                    raise ValueError("mock center cannot feed measured memory")
                self.positions.update(beacons)
                for name, role in (("distance", "hyeol"), ("maek_mic", "maek")):
                    if (name == "distance" and not distance) or (name == "maek_mic" and not mic):
                        continue
                    ch = inputs.get("channels", {}).get(name, {})
                    key = (ch.get("source"), ch.get("seq"))
                    value, age = ch.get("value"), ch.get("age_s")
                    if (not ch.get("connected") or value is None or age is None
                            or not 0 <= age < .5 or self.seen.get(name) == key):
                        continue
                    self.seen[name] = key
                    if name == "distance":
                        if ch.get("source") != "TOFSense (collector)" or not 0 < float(value) < 60:
                            continue
                        self.emit(role, name, {"ceiling_0": float(value)})
                    else:
                        if (not str(ch.get("source", "")).startswith("maek-pi:") or len(value) != 3
                                or not all(isinstance(v, (int, float)) and np.isfinite(v) and 0 <= v <= 1 for v in value)):
                            continue
                        self.emit(role, name, {f"mic_level_{i}": float(v) for i, v in enumerate(value)})
                self.errors.pop("center", None)
            except Exception as exc:
                self.center_valid = False
                self.positions.update({})
                self.errors["center"] = str(exc)
            self.stop.wait(max(.01, .1-(time.monotonic()-begin)))

    def _monitor(self, url):
        import websocket
        url = url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)+"/ws/monitor"
        while not self.stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(url, timeout=1, enable_multithread=True)
                while not self.stop.is_set():
                    ws.send("next")
                    packet = ws.recv()
                    key, features = decode_monitor(packet)
                    if (self.center_valid and self.seen.get("tof") != key
                            and abs(time.time()-key[1]/1000.) < self.config["max_sensor_age_s"]):
                        self.seen["tof"] = key
                        self.emit("hyeol", "tof", features)
                    self.counts["monitor_received"] += 1
                    self.errors.pop("monitor", None)
            except Exception as exc:
                self.errors["monitor"] = str(exc)
            finally:
                if ws:
                    ws.close(timeout=0)
            self.stop.wait(.5)

    def start_collector(self, host, port):
        # Separate from the running center's 9500. One legacy collector per listener.
        listener = socket.socket()
        try:
            listener.bind((host, port))
            listener.listen(1)
            listener.settimeout(.5)
        except Exception:
            listener.close()
            raise
        self.start_thread(self._collector, listener)
        if not getattr(self, "processing", False):
            self.processing = True
            self.start_thread(self._preprocess)
        return listener.getsockname()[1]

    def _collector(self, listener):
        try:
            while not self.stop.is_set():
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                with client:
                    client.settimeout(2)
                    role, rotate, last = "hyeol", self.config["rotate"], {}
                    clock = StreamClock(self.config["max_sensor_age_s"])
                    try:
                        header, _ = recv_packet(client)
                        if header.get("type") != "hello" or header.get("version") != 1:
                            raise ValueError("collector must start with hello version 1")
                        role = header.get("role", "hyeol")
                        rotate = header.get("rotate", rotate)
                        if role not in ("hyeol", "maek") or rotate not in (0, 90, 180, 270):
                            raise ValueError("bad role/rotation")
                        while not self.stop.is_set():
                            h, payload = recv_packet(client)
                            kind = h.get("type")
                            if kind not in ("tof", "lidar", "audio"):
                                continue
                            if (kind == "audio") != (role == "maek"):
                                raise ValueError("sensor/role mismatch")
                            seq = int(h["seq"])
                            if seq <= last.get(kind, -1):
                                self.counts["duplicate_raw"] += 1
                                continue
                            last[kind] = seq
                            clock_mode = clock.accept(h, time.monotonic())
                            self.counts[clock_mode+"_raw"] += 1
                            with self.lock:
                                if kind in self.raw:
                                    self.counts["raw_coalesced"] += 1
                                self.raw[kind] = (time.monotonic(), role, rotate, h, payload)
                            self.counts["raw_received"] += 1
                            self.errors.pop("collector", None)
                    except Exception as exc:
                        self.errors["collector"] = str(exc)
        finally:
            listener.close()

    def _preprocess(self):
        while not self.stop.wait(.005):
            with self.lock:
                latest, self.raw = self.raw, {}
            for kind, (stamp, role, rotate, h, payload) in latest.items():
                if time.monotonic()-stamp > .5:
                    self.counts["stale_raw"] += 1
                    continue
                try:
                    if kind == "tof":
                        f = depth_features(h, payload, rotate, self.config["confidence"])
                    elif kind == "lidar":
                        d = float(h["distance_mm"])/1000.
                        if not h.get("valid") or h.get("status") == 255 or h.get("id") != 0 or not 0 < d < 60:
                            continue
                        f = {"ceiling_0": d}
                    else:
                        if (h.get("sample_rate"), h.get("channels"), h.get("dtype")) != (44100, 3, "<i2") or len(payload) > 264600:
                            raise ValueError("audio must be PCM16 LE, 3ch, 44100 Hz, <=1 second")
                        f = pcm_features(payload)
                    self.emit(role, "raw_"+kind, f)
                    self.errors.pop("preprocess", None)
                except Exception as exc:
                    self.errors["preprocess"] = str(exc)

    def report(self):
        return {"calibrated": self.config["calibrated"], "counts": dict(self.counts),
                "position_mode": self.config.get("position_mode", "range_fit"),
                "memory_namespace": memory_namespace(self.config),
                "position_details": self.positions.report(),
                "inputs": self.input_status,
                "center_poll_age_s": time.monotonic()-self.last_poll if self.last_poll is not None else None,
                "positions_available": {r: self.positions.get(r) is not None for r in ("hyeol", "maek")},
                "errors": dict(self.errors), "center_tof_accepted": self.center_valid,
                "memory_error": self.memory.error if self.memory else None,
                "memory_queue_drops": self.memory.dropped if self.memory else 0,
                "saved_seconds": self.memory.saved_seconds if self.memory else 0}

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(4)
        if any(t.is_alive() for t in self.threads):
            raise RuntimeError("sensor thread did not stop")
