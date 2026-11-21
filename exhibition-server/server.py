#!/usr/bin/env python3
"""Headless Arducam ToF viewer for Raspberry Pi (WebSocket depth stream + MJPEG fallback)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import shutil
import socket
import struct
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import cv2
import numpy as np

import pi_bundle
import audio_assets
from odometry import Odometry, OdometryWorker
from host_stats import HostStats
from inputs import InputChannel, create_channels, start_mock_sources
from mapping_store import MappingStore
from mock_tof import HFOV_DEG, SIM_DISTANCE_HEIGHT, SIM_TOF_HEIGHT, SIM_TOF_PITCH, MockToF
from mock_tof import H as TOF_H, W as TOF_W
from visual_store import VisualStore, v_tof_params
from simulator_store import SimulatorStore
from sim_feed import FRAME_BYTES as SIM_FRAME_BYTES, SimFeed
from venue import VENUES, load_venue


MODES = ("sensor", "contour", "side", "relief")
MAX_RANGE_MM = 4000
INVALID = np.uint16(65535)
SENSOR_SCALE = 3
CONTOUR_SCALE = 4
MIN_CONTOUR_AREA = 12  # native pixels
SLAT_PITCH = 32  # MJPEG output pixels; index.html uses the same ratio of image height
SLAT_GAP = 8
STALE_SECONDS = 3.0
MOCK_FPS = 30
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# magic, mode, flags, width, height, frame_id, capture_ms, center_mm, near, far, line, row %, gain x100, interval,
# side baseline %, padding, side flow x100
HEADER = struct.Struct("<4sBBHHIdfHHBBHHBBH")
# URL -> page file next to server.py (2026-10-15 structure).
#   /            시각화 작업실 (index.html): make ToF visual presets, preview, assign them to the four output slots
#   /operator    연결 관리 (was 출력 관리): server / sensor status, the four slots, output addresses
#   /display/... every real output, one page (display.html) with an output profile from the path:
#                /display/hyeol, /display/maek = the small screen on each device (기구 화면, with its sensor overlay),
#                /display/blind?space=a|b = the space's projector window (블라인드 출력): the blind slot of the role the
#                mapping assignment gives that space, through the booth mapping.
# Compatibility routes (old addresses keep working, not in the menus): /monitor/hyeol, /monitor/maek (= the device
# screens), /output/hyeol, /output/maek (+ ?space=a|b&clean = the blind output of that space; without space: that
# role's blind preset, unmapped). /output/a and /output/b were removed on 2026-10-14 (404).
PAGES = {"/": "index.html", "/operator": "operator.html",
         "/mapping": "mapping.html", "/simulator": "simulator.html",
         "/display/hyeol": "display.html", "/display/maek": "display.html", "/display/blind": "display.html",
         "/output/hyeol": "display.html", "/output/maek": "display.html",
         "/monitor/hyeol": "display.html", "/monitor/maek": "display.html",
         # read-only 3D check of the venue geometry with the live device poses (no editing)
         "/venue3d": "venue3d.html",
         # 심 (central structure) LED matrix simulator: 180 strips / 10,350 LEDs under the tilted top plate (WebGL)
         "/sim": "sim.html",
         # phone remote for the 혈 device screen (2026-11-20): edit the shown composition, saved and sent to the 혈 Pi
         "/remote/hyeol": "remote.html"}
# editing / checking screens (not exhibition outputs): opened without ?test, ?clean or ?embed they redirect to ?test
TOOL_PAGES = {"/", "/operator", "/simulator", "/mapping", "/venue3d", "/sim"}


class SharedState:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.frame: dict[str, object] | None = None
        self.packet: bytes | None = None
        self.monitor_packet: bytes | None = None  # always the measured depth (SENSOR), whatever the output mode
        # ToF layer streams (/ws?tof=<settings>): one per distinct settings with a connected client (compositions'
        # ToF layers in the 작업실 preview and the outputs); key = the validated settings as canonical JSON
        self.preset_clients: dict[str, int] = {}
        self.preset_packets: dict[str, bytes] = {}
        self.venue_name = "test room"
        self.collecting = True  # False while the simulated 혈 is detached (idling): no new ToF frames
        self.hyeol: dict | None = None  # simulator 혈 state (attached / in use or idle), for /status and /inputs
        self.frame_id = 0
        self.frame_time = 0.0
        self.fps = 0.0
        self.process_ms = 0.0
        self.clients = 0
        self.resolution = "waiting"
        self.error: str | None = None
        self.mode = "sensor"
        self.near_mm = 200
        self.far_mm = MAX_RANGE_MM
        self.confidence = 30
        self.interval_mm = 200
        self.line_px = 1
        self.slats = False
        self.vertical = False
        self.nodata = False
        self.temporal = True
        self.row_pct = 50
        self.gain = 1.0
        self.base_pct = 60
        self.flow = 1.0
        self.intrinsics: dict[str, float] | None = None
        self.mock = False
        self.odometry = None   # odometry.Odometry once the first frame arrives (camera_loop), None with --no-odometry
        self.stop = threading.Event()
        self.jpeg_lock = threading.Lock()
        self.jpeg: tuple[int, bytes] = (-1, b"")

    def config(self) -> dict[str, object]:
        with self.condition:
            return {
                "mode": self.mode,
                "near": self.near_mm,
                "far": self.far_mm,
                "confidence": self.confidence,
                "interval": self.interval_mm,
                "line": self.line_px,
                "slats": self.slats,
                "vertical": self.vertical,
                "nodata": self.nodata,
                "temporal": self.temporal,
                "row": self.row_pct,
                "gain": self.gain,
                "base": self.base_pct,
                "flow": self.flow,
            }

    def update_config(self, query: dict[str, list[str]]) -> None:
        with self.condition:
            if "mode" in query and query["mode"][0] in MODES:
                self.mode = query["mode"][0]
            if "near" in query:
                self.near_mm = max(0, min(MAX_RANGE_MM - 100, int(query["near"][0])))
            if "far" in query:
                self.far_mm = max(100, min(MAX_RANGE_MM, int(query["far"][0])))
            self.far_mm = max(self.far_mm, self.near_mm + 100)
            if "confidence" in query:
                self.confidence = max(0, min(255, int(query["confidence"][0])))
            if "interval" in query:
                self.interval_mm = max(50, min(500, round(int(query["interval"][0]) / 50) * 50))
            if "line" in query:
                self.line_px = max(1, min(3, int(query["line"][0])))
            if "slats" in query:
                self.slats = query["slats"][0].lower() in ("1", "true", "on")
            if "vertical" in query:
                self.vertical = query["vertical"][0].lower() in ("1", "true", "on")
            if "nodata" in query:
                self.nodata = query["nodata"][0].lower() in ("1", "true", "on")
            if "temporal" in query:
                self.temporal = query["temporal"][0].lower() in ("1", "true", "on")
            if "row" in query:
                self.row_pct = max(0, min(100, int(query["row"][0])))
            if "gain" in query:
                self.gain = max(0.5, min(3.0, round(float(query["gain"][0]), 1)))
            if "base" in query:
                self.base_pct = max(0, min(90, int(query["base"][0])))
            if "flow" in query:
                self.flow = max(0.25, min(4.0, round(float(query["flow"][0]) * 4) / 4))

    def publish(self, frame: dict[str, object], packet: bytes, fps: float, monitor_packet: bytes | None = None,
                preset_packets: dict[str, bytes] | None = None) -> None:
        with self.condition:
            self.frame = frame
            self.packet = packet
            self.monitor_packet = monitor_packet
            self.preset_packets = preset_packets or {}
            self.frame_id += 1
            self.frame_time = time.monotonic()
            self.fps = fps
            self.process_ms = time.time() * 1000 - float(frame["capture_ms"])
            self.error = None
            self.condition.notify_all()

    def wait_frame(self, last_id: int, timeout: float, monitor: bool = False, preset: str | None = None) -> tuple[int, dict[str, object] | None, bytes | None]:
        with self.condition:
            self.condition.wait_for(lambda: self.frame_id != last_id or self.stop.is_set(), timeout=timeout)
            if preset is not None:
                return self.frame_id, self.frame, self.preset_packets.get(preset)
            return self.frame_id, self.frame, self.monitor_packet if monitor else self.packet

    def subscribe_preset(self, preset: str, delta: int) -> None:
        with self.condition:
            n = self.preset_clients.get(preset, 0) + delta
            if n > 0:
                self.preset_clients[preset] = n
            else:
                self.preset_clients.pop(preset, None)

    def active_presets(self) -> list[str]:
        with self.condition:
            return list(self.preset_clients)

    def jpeg_for(self, frame_id: int, frame: dict[str, object]) -> bytes | None:
        with self.jpeg_lock:
            if self.jpeg[0] != frame_id:
                ok, encoded = cv2.imencode(".jpg", render_image(frame), [cv2.IMWRITE_JPEG_QUALITY, 88])
                if not ok:
                    return None
                self.jpeg = (frame_id, encoded.tobytes())
            return self.jpeg[1]

    def status(self) -> dict[str, object]:
        with self.condition:
            fresh = time.monotonic() - self.frame_time < STALE_SECONDS
            return {
                "connected": self.frame is not None and self.error is None and fresh and self.collecting,
                "collecting": self.collecting,
                "frame_id": self.frame_id,
                "hyeol": self.hyeol,
                "fps": round(self.fps, 1) if fresh else 0.0,
                "process_ms": round(self.process_ms, 1),
                "clients": self.clients,
                "resolution": self.resolution,
                "mode": self.mode,
                "near_mm": self.near_mm,
                "far_mm": self.far_mm,
                "confidence": self.confidence,
                "interval_mm": self.interval_mm,
                "line_px": self.line_px,
                "slats": self.slats,
                "vertical": self.vertical,
                "nodata": self.nodata,
                "temporal": self.temporal,
                "row_pct": self.row_pct,
                "gain": self.gain,
                "base_pct": self.base_pct,
                "flow": self.flow,
                "intrinsics": self.intrinsics,
                "mock": self.mock,
                "error": self.error,
            }


def valid_mask(depth: np.ndarray, confidence: np.ndarray, far_mm: int, confidence_min: int) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return np.isfinite(depth) & (depth > 0) & (depth <= far_mm) & (confidence >= confidence_min)


def center_distance(depth: np.ndarray, valid: np.ndarray) -> float:
    cy, cx = depth.shape[0] // 2, depth.shape[1] // 2
    patch = depth[cy - 2:cy + 3, cx - 2:cx + 3][valid[cy - 2:cy + 3, cx - 2:cx + 3]]
    return float(np.median(patch)) if patch.size >= 6 else float("nan")


def format_distance(mm: float) -> str:
    if not np.isfinite(mm):
        return "-- mm"
    return f"{mm:.0f} mm" if mm < 1000 else f"{mm / 1000:.2f} m"


class TemporalFilter:
    def __init__(self) -> None:
        self.history: list[np.ndarray] = []

    def apply(self, depth: np.ndarray, valid: np.ndarray, temporal: bool) -> np.ndarray:
        current = np.where(valid, depth, 0).astype(np.uint16)
        current[~valid] = INVALID
        if self.history and self.history[-1].shape != current.shape:
            self.history.clear()
        self.history = (self.history + [current])[-3:]
        if temporal and len(self.history) == 3:
            a, b, c = self.history
            current = np.maximum(np.minimum(a, b), np.minimum(np.maximum(a, b), c))
        return cv2.medianBlur(current, 5)


def contour_lines(filtered: np.ndarray, near_mm: int, far_mm: int, interval: int) -> tuple[list[np.ndarray], list[int]]:
    known = filtered[filtered != INVALID]
    if not known.size:
        return [], []
    lo = max(near_mm, int(known.min()))
    hi = min(far_mm, int(known.max()))
    lines, levels = [], []
    for level in range((lo // interval + 1) * interval, (hi // interval + 1) * interval + 1, interval):
        contours, _ = cv2.findContours((filtered < level).astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        kept = [c.reshape(-1, 2) for c in contours if cv2.contourArea(c) >= MIN_CONTOUR_AREA]
        lines += kept
        levels += [level] * len(kept)
    return lines, levels


def build_packet(frame: dict[str, object], frame_id: int) -> bytes:
    h, w = frame["shape"]
    header = HEADER.pack(
        b"TOF3",
        MODES.index(frame["mode"]),
        (1 if frame["slats"] else 0) | (2 if frame["vertical"] else 0) | (4 if frame["nodata"] else 0),
        w,
        h,
        frame_id & 0xFFFFFFFF,
        frame["capture_ms"],
        frame["center_mm"],
        frame["near"],
        frame["far"],
        frame["line"],
        frame["row"],
        round(frame["gain"] * 100),
        frame["interval"],
        frame["base"],
        0,
        round(frame["flow"] * 100),
    )
    if "depth" in frame:
        return header + frame["depth"].tobytes()
    lines = frame["lines"]
    counts = np.array([len(c) for c in lines], dtype="<u4")
    levels = np.array(frame["levels"], dtype="<u2")
    coords = np.concatenate(lines).astype("<u2") if lines else np.zeros(0, dtype="<u2")
    return header + struct.pack("<I", len(lines)) + counts.tobytes() + levels.tobytes() + coords.tobytes()


def render_image(frame: dict[str, object]) -> np.ndarray:
    h, w = frame["shape"]
    if "depth" in frame:
        depth = frame["depth"].astype(np.float32)
        near, far = frame["near"], frame["far"]
        scaled = 1.0 - np.clip((depth - near) / max(1, far - near), 0.0, 1.0)
        gray = np.where(depth > 0, scaled * 255, 0).astype(np.uint8)
        s = SENSOR_SCALE
        image = cv2.resize(gray, (w * s, h * s), interpolation=cv2.INTER_NEAREST)
        if frame["mode"] == "side":
            if frame["vertical"]:
                x = min(w - 1, round(frame["row"] / 100 * (w - 1))) * s + s // 2
                cv2.line(image, (x, 0), (x, image.shape[0] - 1), 255, 1)
            else:
                y = min(h - 1, round(frame["row"] / 100 * (h - 1))) * s + s // 2
                cv2.line(image, (0, y), (image.shape[1] - 1, y), 255, 1)
            return image
        white, black = 255, 0  # no-data pixels stay black (the old magenta marking was removed)
        cy, cx = h // 2, w // 2
        x0, y0, x1, y1 = (cx - 2) * s - 2, (cy - 2) * s - 2, (cx + 3) * s + 1, (cy + 3) * s + 1
        cv2.rectangle(image, (x0, y0), (x1, y1), black, 3)
        cv2.rectangle(image, (x0, y0), (x1, y1), white, 1)
        label = format_distance(frame["center_mm"])
        font, font_scale, thickness = cv2.FONT_HERSHEY_SIMPLEX, 1.1, 2
        (tw, th), _ = cv2.getTextSize(label, font, font_scale, thickness)
        tx, ty = (image.shape[1] - tw) // 2, image.shape[0] - 24
        cv2.rectangle(image, (tx - 10, ty - th - 10), (tx + tw + 10, ty + 12), black, -1)
        cv2.putText(image, label, (tx, ty), font, font_scale, white, thickness, cv2.LINE_AA)
        return image

    s, line = CONTOUR_SCALE, frame["line"]
    image = np.zeros((h * s, w * s), dtype=np.uint8)
    lines = [((c.astype(np.float32) + 0.5) * s).astype(np.int32) for c in frame["lines"]]
    cv2.polylines(image, lines, True, 255, line, cv2.LINE_AA)
    edge = s // 2 + line + 1
    image[:edge] = 0
    image[-edge:] = 0
    image[:, :edge] = 0
    image[:, -edge:] = 0
    if frame["slats"]:
        image[(np.arange(image.shape[0]) % SLAT_PITCH) >= SLAT_PITCH - SLAT_GAP] = 0
    return image


def build_output(cfg: dict[str, object], depth: np.ndarray, confidence: np.ndarray, capture_ms: float,
                 temporal: "TemporalFilter", frame_id: int) -> tuple[dict[str, object], bytes]:
    """One frame for one set of ToF settings (the /config values or a visual preset's params): validity, temporal and
    median filter, contour lines. The global stream (/ws) and every preset stream (/ws?preset=) use this same code."""
    valid = valid_mask(depth, confidence, int(cfg["far"]), int(cfg["confidence"]))
    out = dict(cfg, shape=depth.shape, capture_ms=capture_ms, center_mm=center_distance(depth, valid))
    if cfg["mode"] == "sensor":
        measured = valid_mask(depth, confidence, int(INVALID) - 1, int(cfg["confidence"]))
        out["depth"] = np.where(measured, depth, 0).astype("<u2")
    else:
        filtered = temporal.apply(depth, valid, bool(cfg["temporal"]))
        # pixels with no valid measurement in this frame stay "no data" (black on screen): the temporal median
        # and the 5×5 median must not carry an older value (afterimage) or fill a hole from its neighbours
        filtered = np.where(valid, filtered, INVALID)
        if cfg["mode"] == "side":
            out["depth"] = np.where(filtered == INVALID, 0, filtered).astype("<u2")
        else:
            lines, levels = contour_lines(filtered, int(cfg["near"]), int(cfg["far"]), int(cfg["interval"]))
            out["lines"], out["levels"] = lines, levels
    return out, build_packet(out, frame_id)


def stream_key(query_value: str) -> str:
    """/ws?tof=<JSON settings> → the validated settings as canonical JSON (raises ValueError when invalid)."""
    return json.dumps(v_tof_params(json.loads(query_value), with_mode=True), sort_keys=True, separators=(",", ":"))


def preset_config(params: dict) -> dict[str, object]:
    """ToF layer settings in the /config shape (nodata is no longer used: no-data is always black)."""
    return {k: params[k] for k in ("mode", "near", "far", "confidence", "interval", "line", "slats", "vertical",
                                   "temporal", "row", "gain", "base", "flow")} | {"nodata": False}


def pose_trailer(pose: dict) -> bytes:
    """혈 움직임 추정 (odometry.py) after a depth packet: "POSE" + x, y (mm), heading (°), quality (0–1) as float32.
    Pages that do not know it ignore it (they read exactly w × h depth values)."""
    return struct.pack("<4sffff", b"POSE", pose["x"], pose["y"], pose["heading"], pose["quality"])


class IrEdges:
    """적외선 윤곽 (2026-11-11): the ToF confidence (IR brightness) shows materials and object outlines far better than
    the depth does (a pillar and the wall behind it are at almost the same distance). Canny on the smoothed brightness
    gives the outline; it is traced into strokes (findContours), short crumbs dropped, each stroke smoothed (moving
    average) and thinned, and every point carries the depth there (the nearest measured depth within 5×5, so an
    outline on a dark object still gets the depth of its lit side). A stroke is cut where the depth jumps.
    Sent after the depth picture as "EDGL" + strokes (u32) + points (u32) + count[strokes] (u16) + x·8[points],
    y·8[points], depth mm[points] (u16). Pages that do not know it ignore it."""
    LOW, HIGH, CONF, MIN_LEN, SMOOTH, STEP, JUMP, MAX_POINTS, MAX_STROKES = 20, 50, 30, 16, 5, 2, 300, 12000, 120

    def __init__(self) -> None:
        self.ema = None

    def trailer(self, depth: np.ndarray, confidence: np.ndarray) -> bytes:
        amp = np.clip(confidence.astype(np.float32) * 2, 0, 255)
        self.ema = amp if self.ema is None or self.ema.shape != amp.shape else 0.6 * self.ema + 0.4 * amp
        a8 = cv2.bilateralFilter(self.ema, 7, 30, 5).clip(0, 255).astype(np.uint8)
        edges = cv2.Canny(cv2.GaussianBlur(a8, (5, 5), 0), self.LOW, self.HIGH)
        near = np.where((depth > 0) & (depth <= MAX_RANGE_MM) & (confidence >= self.CONF), depth, 65535).astype(np.uint16)
        near = cv2.erode(near, np.ones((5, 5), np.uint8))   # nearest measured depth within 5×5
        h, w = near.shape
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        counts, xs, ys, zs, total = [], [], [], [], 0
        k = np.ones(self.SMOOTH, np.float32) / self.SMOOTH
        for c in sorted(contours, key=len, reverse=True)[:self.MAX_STROKES]:
            if len(c) < self.MIN_LEN or total >= self.MAX_POINTS:
                continue
            pts = c[:, 0, :].astype(np.float32)
            if len(pts) > self.SMOOTH:   # moving average along the stroke (wraps: findContours strokes are loops)
                pad = np.concatenate([pts[-(self.SMOOTH // 2):], pts, pts[:self.SMOOTH // 2]])
                pts = np.stack([np.convolve(pad[:, 0], k, "valid"), np.convolve(pad[:, 1], k, "valid")], 1)
            pts = pts[::self.STEP]
            z = near[np.clip(np.rint(pts[:, 1]).astype(int), 0, h - 1), np.clip(np.rint(pts[:, 0]).astype(int), 0, w - 1)].astype(np.int32)
            # cut into pieces where there is no depth or the depth jumps
            ok = z < 65535
            brk = np.ones(len(z), bool); brk[1:] = (~ok[1:]) | (~ok[:-1]) | (np.abs(np.diff(z)) > self.JUMP)
            starts = np.flatnonzero(brk)
            for i, st in enumerate(starts):
                en = starts[i + 1] if i + 1 < len(starts) else len(z)
                if not ok[st] or en - st < 3:
                    continue
                counts.append(en - st); xs.append(pts[st:en, 0]); ys.append(pts[st:en, 1]); zs.append(z[st:en]); total += en - st
        if not counts:
            return struct.pack("<4sII", b"EDGL", 0, 0)
        x = np.clip(np.concatenate(xs) * 8, 0, 65535).astype("<u2"); y = np.clip(np.concatenate(ys) * 8, 0, 65535).astype("<u2")
        return (struct.pack("<4sII", b"EDGL", len(counts), x.size) + np.array(counts, "<u2").tobytes()
                + x.tobytes() + y.tobytes() + np.concatenate(zs).astype("<u2").tobytes())


class IsoLines:
    """거리 등고선 (2026-11-11), server side: depth averaged over time (EMA) and space (normalized Gaussian, which also
    fills pinholes), then for every level a region boundary is traced with findContours (closed shapes), smoothed
    (moving average around the loop), crumbs dropped; parts along "no measurement" are cut out. Every point is at
    the level's distance. Sent as "ISOL" in the same layout as "EDGL"."""
    NEAR, FAR, STEP, CONF, SIGMA, MIN_W, MIN_LEN, SMOOTH, PSTEP, MAX_POINTS, EDGE_GAP, MIN_PIECE = 400, 3600, 150, 30, 2.0, 0.2, 20, 5, 2, 16000, 1, 8
    STEP_FAR = 0   # > 0: spacing grows from STEP (near) to STEP_FAR (far) — denser lines close by, a sense of depth

    def __init__(self) -> None:
        self.num = self.den = None

    def trailer(self, depth: np.ndarray, confidence: np.ndarray) -> bytes:
        valid = ((depth > 0) & (depth <= MAX_RANGE_MM) & (confidence >= self.CONF)).astype(np.float32)
        d = np.where(valid > 0, depth, 0).astype(np.float32)
        if self.num is None or self.num.shape != d.shape:
            self.num, self.den = d * valid, valid.copy()
        else:   # time: exponential average of depth·weight and weight (a pixel without a measurement keeps its past)
            self.num = 0.65 * self.num + 0.35 * d * valid; self.den = 0.65 * self.den + 0.35 * valid
        num = cv2.GaussianBlur(self.num, (0, 0), self.SIGMA); den = cv2.GaussianBlur(self.den, (0, 0), self.SIGMA)
        ok = den > self.MIN_W
        sm = np.where(ok, num / np.maximum(den, 1e-6), 0).astype(np.float32)
        h, w = sm.shape
        okm = ok.astype(np.uint8)
        counts, xs, ys, zs, total = [], [], [], [], 0
        k = np.ones(self.SMOOTH, np.float32) / self.SMOOTH
        levels, lv = [], float(self.NEAR)
        while lv <= self.FAR:
            levels.append(int(lv))
            t = (lv - self.NEAR) / max(1, self.FAR - self.NEAR)
            lv += self.STEP + (max(self.STEP_FAR, self.STEP) - self.STEP) * t if self.STEP_FAR > 0 else self.STEP
        for level in levels:
            mask = ((sm < level) & ok).astype(np.uint8)
            if not mask.any():
                continue
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
            for c in contours:
                if len(c) < self.MIN_LEN or total >= self.MAX_POINTS:
                    continue
                pts = c[:, 0, :].astype(np.float32)
                ix, iy = pts[:, 0].astype(int), pts[:, 1].astype(int)
                # keep only where both sides were measured (a boundary against "no measurement" is not a distance line)
                g = self.EDGE_GAP
                inner = okm[np.clip(iy - g, 0, h - 1), ix] & okm[np.clip(iy + g, 0, h - 1), ix] & okm[iy, np.clip(ix - g, 0, w - 1)] & okm[iy, np.clip(ix + g, 0, w - 1)]
                inner &= (ix > 1) & (ix < w - 2) & (iy > 1) & (iy < h - 2)
                pad = np.concatenate([pts[-(self.SMOOTH // 2):], pts, pts[:self.SMOOTH // 2]])
                pts = np.stack([np.convolve(pad[:, 0], k, "valid"), np.convolve(pad[:, 1], k, "valid")], 1)
                pts, inner = pts[::self.PSTEP], inner[::self.PSTEP].astype(bool)
                brk = np.ones(len(pts), bool); brk[1:] = ~inner[1:] | ~inner[:-1]
                starts = np.flatnonzero(brk)
                for i, st in enumerate(starts):
                    en = starts[i + 1] if i + 1 < len(starts) else len(pts)
                    if not inner[st] or en - st < self.MIN_PIECE:
                        continue
                    if st == 0 and en == len(pts) and inner.all():   # a whole closed shape: close it
                        seg = np.vstack([pts, pts[:1]])
                    else:
                        seg = pts[st:en]
                    counts.append(len(seg)); xs.append(seg[:, 0]); ys.append(seg[:, 1]); zs.append(np.full(len(seg), level)); total += len(seg)
        if not counts:
            return struct.pack("<4sII", b"ISOL", 0, 0)
        x = np.clip(np.concatenate(xs) * 8, 0, 65535).astype("<u2"); y = np.clip(np.concatenate(ys) * 8, 0, 65535).astype("<u2")
        return (struct.pack("<4sII", b"ISOL", len(counts), x.size) + np.array(counts, "<u2").tobytes()
                + x.tobytes() + y.tobytes() + np.concatenate(zs).astype("<u2").tobytes())


class IrEdgesWorker:
    """IrEdges on its own thread with the newest frame only (like OdometryWorker): the camera loop never waits;
    latest() is the last finished trailer (a frame behind)."""

    def __init__(self, stop: threading.Event) -> None:
        self.edges, self.iso, self.stop, self.cond, self.job, self.out = IrEdges(), IsoLines(), stop, threading.Condition(), None, b""
        threading.Thread(target=self._run, daemon=True, name="ir-edges").start()

    def submit(self, depth: np.ndarray, confidence: np.ndarray) -> None:
        with self.cond:
            self.job = (depth, confidence)
            self.cond.notify()

    def latest(self) -> bytes:
        return self.out

    TUNE = Path(__file__).resolve().parent.parent / "run" / "lines.json"   # 즉시 조절: {"edges": {"LOW": 15}, "iso": {"MIN_LEN": 10}}

    def _tune(self) -> None:
        try:
            mt = self.TUNE.stat().st_mtime
        except OSError:
            return
        if mt == getattr(self, "_mt", None):
            return
        self._mt = mt
        try:
            cfg = json.loads(self.TUNE.read_text(encoding="utf-8"))
            for name, obj in (("edges", self.edges), ("iso", self.iso)):
                for k, v in (cfg.get(name) or {}).items():
                    if k.isupper() and hasattr(type(obj), k):
                        setattr(obj, k, type(getattr(type(obj), k))(v))
            print(f"선 계산 설정 바꿈: {cfg}", flush=True)
        except (OSError, ValueError, TypeError) as exc:
            print(f"선 계산 설정 읽기 실패: {exc}", flush=True)

    def _run(self) -> None:
        last_tune = 0.0
        while not self.stop.is_set():
            with self.cond:
                self.cond.wait_for(lambda: self.job is not None or self.stop.is_set(), timeout=0.5)
                job, self.job = self.job, None
            if time.monotonic() - last_tune > 1.0:
                self._tune(); last_tune = time.monotonic()
            if job is not None:
                try:
                    self.out = self.edges.trailer(*job) + self.iso.trailer(*job)
                except Exception as exc:   # never take the camera down
                    print(f"적외선 윤곽 오류: {exc}", flush=True)


def with_pose(packet: bytes, trailer: bytes | None) -> bytes:
    if trailer is None or packet[4] not in (MODES.index("sensor"), MODES.index("side")):
        return packet   # contour / relief packets carry lines, not the depth picture
    return packet + trailer


def camera_loop(state: SharedState, mock: bool, simulator: SimulatorStore | None = None, venue=None,
                visual: "VisualStore | None" = None, camera_index: int = 0, odometry: bool = True,
                source: str = "direct") -> None:
    camera = None
    started_camera = False
    temporal = TemporalFilter()
    preset_filters: dict[str, tuple[tuple, TemporalFilter]] = {}  # per streamed preset: (its settings, its own filter)
    ir_edges = None
    # sensor-like test frames (mock_tof.py), not a recording; with a venue the camera stands in its geometry
    mock_camera = MockToF(venue=venue) if mock else None
    # a pose change re-traces the scene: ~35 ms in the test room (at most 10×/s), ~2 ms in a venue (every frame)
    pose_interval = 0.1 if venue is None else 0.0
    sim_version, sim_applied_at = None, 0.0
    try:
        if not mock:
            if source == "collector":   # 수집단(collector) 소켓 · 또는 혈 Pi 송신(TCP, COLLECTOR_LISTEN) 에서 받는다 (collector_client.py)
                import collector_client as ac
            else:
                import ArducamDepthCamera as ac

            camera = ac.ArducamCamera()
            result = camera.open(ac.Connection.CSI, camera_index)
            if result != 0:
                camera = None
                raise RuntimeError(f"camera open failed: {result}")
            result = camera.start(ac.FrameType.DEPTH)
            if result != 0:
                raise RuntimeError(f"camera start failed: {result}")
            started_camera = True
            camera.setControl(ac.Control.RANGE, MAX_RANGE_MM)
            info = camera.getCameraInfo()
            state.resolution = f"{info.width}x{info.height}"
            print(f"Camera connected: {state.resolution}", flush=True)
            try:
                state.intrinsics = {
                    k: camera.getControl(getattr(ac.Control, f"INTRINSIC_{k.upper()}")) / 100
                    for k in ("fx", "fy", "cx", "cy")
                }
                print(f"Intrinsics (SDK, /100): {state.intrinsics}", flush=True)
            except Exception as exc:
                print(f"Intrinsics unavailable: {exc}", flush=True)
        else:
            state.resolution = "240x180 mock"

        count = 0
        started = next_tick = time.monotonic()
        while not state.stop.is_set():
            if mock:
                next_tick += 1 / MOCK_FPS
                time.sleep(max(0.0, next_tick - time.monotonic()))
                next_tick = max(next_tick, time.monotonic() - 1 / MOCK_FPS)
                capture_ms = time.time() * 1000
                # 혈 detached in the simulator (idling): no frames are collected until it is attached again
                if simulator is not None:
                    hyeol = simulator.hyeol_state()
                    with state.condition:
                        state.hyeol, state.collecting = hyeol, hyeol["collecting"]
                    if not hyeol["collecting"]:
                        continue
                # The 혈 pose from the simulator moves the mock camera (ToF fixed at 1.5 m, facing forward).
                if simulator is not None and simulator.version != sim_version and time.monotonic() - sim_applied_at >= pose_interval:
                    sim_version, sim_applied_at = simulator.version, time.monotonic()
                    pose = simulator.tof_pose()   # at the back of 혈, looking backward (2026-11-06)
                    mock_camera.set_pose(pose["x"], pose["y"], pose["heading"], SIM_TOF_HEIGHT, SIM_TOF_PITCH)
                # mock-up people: the same positions the simulator state sends to /venue3d
                depth, confidence = mock_camera.frame(time.monotonic(), simulator.people_for_tof() if simulator is not None else None)
            else:
                frame = camera.requestFrame(2000)
                if frame is None:
                    continue
                capture_ms = time.time() * 1000
                try:
                    depth = np.array(frame.depth_data, dtype=np.float32, copy=True)
                    confidence = np.array(frame.confidence_data, dtype=np.float32, copy=True)
                finally:
                    camera.releaseFrame(frame)

            count += 1
            elapsed = max(0.001, time.monotonic() - started)
            fps = count / elapsed
            if elapsed >= 2.0:
                count = 0
                started = time.monotonic()

            cfg = state.config()
            frame_id = state.frame_id + 1
            out, packet = build_output(cfg, depth, confidence, capture_ms, temporal, frame_id)
            measured = valid_mask(depth, confidence, int(INVALID) - 1, int(cfg["confidence"]))
            measured_depth = np.where(measured, depth, 0).astype("<u2")
            trailer = None
            if ir_edges is None:
                ir_edges = IrEdgesWorker(state.stop)
            ir_edges.submit(depth, confidence)
            if odometry:   # 혈 움직임 추정 from the depth itself (the mock simulation starts it at the simulator's ToF pose)
                if state.odometry is None:   # on its own thread: a slow match never holds up the camera
                    state.odometry = OdometryWorker(Odometry(simulator.tof_pose() if simulator is not None else None), state.stop)
                state.odometry.submit(depth, measured)
                trailer = pose_trailer(state.odometry.pose())
            trailer = (trailer or b"") + ir_edges.latest()   # 적외선 윤곽 (IrEdgesWorker, a frame behind)
            packet = with_pose(packet, trailer)
            # sensing monitor stream (/ws/monitor): the measured depth in SENSOR form, independent of the output mode
            monitor_packet = with_pose(build_packet(dict(out, mode="sensor", depth=measured_depth), frame_id), trailer)
            # ToF layer streams with a connected client: the same processing with that layer's settings (settings
            # equal to the current /config values reuse that packet)
            preset_packets = {}
            if visual is not None:
                for pid in state.active_presets():
                    pcfg = preset_config(json.loads(pid))
                    if pcfg == cfg:
                        preset_packets[pid] = packet   # (already carries the pose)
                        continue
                    key = tuple(sorted(pcfg.items()))
                    if pid not in preset_filters or preset_filters[pid][0] != key:
                        preset_filters[pid] = (key, TemporalFilter())
                    preset_packets[pid] = with_pose(build_output(pcfg, depth, confidence, capture_ms, preset_filters[pid][1], frame_id)[1], trailer)
            state.publish(out, packet, fps, monitor_packet, preset_packets)
    except Exception as exc:  # Keep the web status page available on camera errors.
        print(f"Camera error: {exc}", flush=True)
        with state.condition:
            state.error = str(exc)
            state.condition.notify_all()
    finally:
        if camera is not None:
            try:
                if started_camera:
                    camera.stop()
            except Exception as exc:
                print(f"Camera stop error: {exc}", flush=True)
            finally:
                camera.close()
                print("Camera closed", flush=True)


def markdown_lite(text: str) -> str:
    """Headings (# to ####) and "- " lists only; everything else is a paragraph. All text is HTML-escaped."""
    out, in_list = [], False
    for raw in text.splitlines():
        line = raw.rstrip()
        item = line.lstrip()
        if item.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{html.escape(item[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line:
            continue
        level = len(line) - len(line.lstrip("#"))
        if 1 <= level <= 4 and line[level:level + 1] == " ":
            out.append(f"<h{level}>{html.escape(line[level + 1:])}</h{level}>")
        else:
            out.append(f"<p>{html.escape(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


# User-facing pages shown in the /progress menu. Add every new user-facing HTML page or screen route here
# (README rule); JSON endpoints such as /inputs do not belong in this list.
NAV_LINKS = (
    ("/progress", "진행상황"),
    ("/", "시각화 작업실"),
    ("/operator", "연결 관리"),
    ("/simulator", "시뮬레이터"),
    ("/mapping", "블라인드 맵핑"),
    ("/venue3d", "공간 3D 확인"),
    ("/sim", "심 LED 시뮬레이터"),
    ("/remote/hyeol", "혈 화면 조종 (폰)"),
)
# The outputs themselves (device screens, blind outputs) and the compatibility routes are opened from 출력 관리.


def test_href(path: str) -> str:
    """Links from /progress open screens in the test UI state (?test); ?clean is the exhibition output."""
    return path if path == "/progress" else path + "?test"


# Sections that must be easy to reach while scrolling (highlighted block + jump chip accent).
KEY_SECTIONS = {"현재 상태": "state", "막힌 것": "blocked", "다음 한 가지": "next", "사용자 결정이 필요한 것": "decide"}

# PROGRESS.md keeps one "### " block per work unit under "## 작업 내역", newest first. Only the newest block is shown open
# (as "최신 작업 내역"); the older blocks are moved into "이전 작업 내역" as closed <details>, so writing a new block at the
# top folds the previous one without editing it. In every other section the text before its first "### " is shown and
# each "### " subsection is a closed <details> (long reference lists stay one tap away).
HISTORY_SECTION = "작업 내역"


def split_subsections(lines: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    head, subs = [], []
    for line in lines:
        if line.startswith("### "):
            subs.append((line[4:].strip(), []))
        elif subs:
            subs[-1][1].append(line)
        else:
            head.append(line)
    return head, subs


def fold(title: str, lines: list[str], fid: str) -> str:
    return (f'<details class="fold" id="{fid}"><summary><span>{html.escape(title)}</span></summary>'
            f'<div class="fold-body">\n{markdown_lite(chr(10).join(lines))}\n</div></details>')


def progress_sections(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Split PROGRESS.md at "## " headings: (intro html, [(title, kind, html)]). Text is rendered by markdown_lite."""
    intro, raw, title, chunk = [], [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if title is None:
                intro = chunk
            else:
                raw.append((title, chunk))
            title, chunk = line[3:].strip(), []
        else:
            chunk.append(line)
    if title is None:
        intro = chunk
    else:
        raw.append((title, chunk))
    sections, older = [], ""
    for i, (t, lines) in enumerate(raw):
        head, subs = split_subsections(lines)
        if t == HISTORY_SECTION:
            latest = ""
            if subs:
                latest = f'<h3 class="entry">{html.escape(subs[0][0])}</h3>\n{markdown_lite(chr(10).join(subs[0][1]))}'
            sections.append(("최신 작업 내역", "latest", markdown_lite("\n".join(head)) + latest))
            if len(subs) > 1:
                older = (f'<p class="fold-note">이전 작업 {len(subs) - 1}개 · 누르면 펼쳐짐 (최신이 위)</p>\n'
                         + "\n".join(fold(st, sl, f"hist-{j}") for j, (st, sl) in enumerate(subs[1:], 1)))
            continue
        body = markdown_lite("\n".join(head)) + "".join("\n" + fold(st, sl, f"sub-{i}-{j}") for j, (st, sl) in enumerate(subs, 1))
        sections.append((t, KEY_SECTIONS.get(t, ""), body))
    if older:
        # older work goes right after the decisions (the last of the sections that must stay on top)
        at = next((k + 1 for k, (_, kind, _) in enumerate(sections) if kind == "decide"), len(sections))
        sections.insert(at, ("이전 작업 내역", "history", older))
    return markdown_lite("\n".join(intro)), sections


def progress_page(path: Path, mock: bool, tof_connected: bool, venue_name: str = "") -> str:
    try:
        intro, sections = progress_sections(path.read_text(encoding="utf-8"))
        updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    except OSError:
        intro, sections, updated = "<p>PROGRESS.md 파일이 없습니다.</p>", [], "--"
    current = ' aria-current="page"'
    nav = "\n".join(
        f'<li><a href="{test_href(u)}"{current if u == "/progress" else ""}>'
        f'{html.escape(label)}</a></li>'
        for u, label in NAV_LINKS
    )
    chips = "\n".join(
        f'<a class="{kind}" href="#sec-{i}">{html.escape(t)}</a>' for i, (t, kind, _) in enumerate(sections, 1)
    )
    blocks = "\n".join(
        f'<section class="sec {kind}" id="sec-{i}" aria-labelledby="sec-{i}-h">'
        f'<h2 id="sec-{i}-h">{html.escape(t)}</h2>\n{body}</section>'
        for i, (t, kind, body) in enumerate(sections, 1)
    )
    # The current action comes first: a copy of the "다음 한 가지" section (same text as the section below).
    now = next((body for t, kind, body in sections if kind == "next"), "")
    now_block = f'<section class="now" aria-label="지금 할 일"><h2>지금 할 일 · 다음 한 가지</h2>\n{now}</section>' if now else ""
    state = (f'{"mock 모드" if mock else "실제 장치 모드"} · {html.escape(venue_name)} · ToF {"연결됨" if tof_connected else "끊김"}'
             f' · 수정 {updated}')
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#000000">
<title>Ripples · Progress</title>
<style>
  :root {{ color-scheme: dark; --line: #333; --text: #eee; --sub: #c8c8c8; --bad: #ff8a7a; --head-h: 56px;
          --sl: env(safe-area-inset-left, 0px); --sr: env(safe-area-inset-right, 0px); }}
  * {{ box-sizing: border-box; }}
  html {{ -webkit-text-size-adjust: 100%; scroll-padding-top: calc(var(--head-h) + 60px + env(safe-area-inset-top, 0px)); }}
  body {{ margin: 0; background: #000; color: var(--text); font: 16px/1.65 -apple-system, system-ui, sans-serif;
         overflow-wrap: anywhere; word-break: break-word; }}
  a {{ color: #fff; text-underline-offset: 3px; }}
  a:focus-visible, button:focus-visible {{ outline: 2px solid #fff; outline-offset: 2px; }}

  header {{ position: sticky; top: 0; z-index: 10; background: #000; border-bottom: 1px solid var(--line);
           padding-top: env(safe-area-inset-top, 0px); }}
  .wrap {{ max-width: 760px; margin: 0 auto; padding-left: calc(16px + var(--sl)); padding-right: calc(12px + var(--sr)); }}
  .bar {{ display: flex; align-items: center; gap: 12px; min-height: var(--head-h); padding-top: 6px; padding-bottom: 6px; }}
  .title {{ flex: 1; min-width: 0; }}
  .title strong {{ display: block; font-size: 18px; line-height: 1.25; }}
  .state {{ display: block; font-size: 14px; line-height: 1.35; color: var(--sub); }}
  .state.bad {{ color: var(--bad); }}
  #menu-button {{ flex: none; min-width: 44px; min-height: 44px; padding: 0 12px; display: inline-flex; align-items: center; gap: 6px;
                 background: #111; color: #fff; border: 1px solid #666; border-radius: 6px; font-family: inherit; font-weight: 600; font-size: 15px; line-height: 1; cursor: pointer; }}
  #menu-button svg {{ width: 20px; height: 20px; }}
  #menu {{ display: none; padding-bottom: 8px; }}
  #menu.open {{ display: block; overflow-y: auto; overscroll-behavior: contain; -webkit-overflow-scrolling: touch; }}
  #menu ul {{ list-style: none; margin: 0; padding: 0; }}
  #menu a {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 48px; padding: 8px 4px; }}
  #menu a[aria-current="page"] {{ text-decoration: none; font-weight: 700; }}
  #menu a[aria-current="page"]::after {{ content: "현재 화면"; font-weight: 400; font-size: 14px; color: var(--sub); }}
  #scrim {{ display: none; position: fixed; inset: 0; z-index: 5; }}
  #scrim.open {{ display: block; }}

  .jump {{ display: flex; gap: 18px; overflow-x: auto; scrollbar-width: none; }}
  .jump::-webkit-scrollbar {{ display: none; }}
  .jump a {{ flex: none; display: inline-flex; align-items: center; justify-content: center; min-height: 44px; min-width: 44px; font-size: 15px; white-space: nowrap; }}
  .jump a.next, .jump a.blocked {{ font-weight: 700; }}

  main {{ padding-top: 16px; padding-bottom: calc(40px + env(safe-area-inset-bottom, 0px)); }}
  .doc-head h1 {{ font-size: 22px; line-height: 1.3; margin: 4px 0 4px; }}
  .now {{ margin: 16px 0 8px; padding: 2px 0 2px 14px; border-left: 3px solid #fff; }}
  .now h2, .sec h2 {{ font-size: 18px; line-height: 1.35; margin: 0 0 6px; }}
  .sec {{ margin-top: 36px; }}
  .sec h3 {{ font-size: 16px; margin: 18px 0 2px; }}
  .sec h3.entry {{ font-size: 17px; margin: 8px 0 4px; }}
  h4 {{ font-size: 15px; margin: 14px 0 2px; color: var(--sub); }}
  .sec.latest {{ padding-left: 14px; border-left: 3px solid #fff; }}
  /* closed toggles: the whole summary row is the tap target (48px+), arrow shows open / closed */
  details.fold {{ margin: 8px 0; border: 1px solid #444; border-radius: 6px; }}
  details.fold > summary {{ list-style: none; display: flex; align-items: center; gap: 10px; min-height: 48px; padding: 10px 12px;
                           cursor: pointer; font-weight: 600; line-height: 1.4; -webkit-tap-highlight-color: transparent; }}
  details.fold > summary::-webkit-details-marker {{ display: none; }}
  details.fold > summary::before {{ content: "▸"; flex: none; width: 1em; color: var(--sub); }}
  details.fold[open] > summary::before {{ content: "▾"; }}
  details.fold[open] > summary {{ border-bottom: 1px solid #333; }}
  details.fold > summary:focus-visible {{ outline: 2px solid #fff; outline-offset: 2px; }}
  .fold-body {{ padding: 4px 12px 10px; }}
  .fold-note {{ color: var(--sub); font-size: 15px; }}
  ul {{ margin: 4px 0; padding-left: 1.2em; }}
  li {{ margin: 4px 0; }}
  p {{ margin: 6px 0; }}

  @media (min-width: 900px) {{
    #menu-button, #scrim {{ display: none !important; }}
    #menu {{ display: block; padding-bottom: 0; }}
    #menu ul {{ display: flex; flex-wrap: wrap; gap: 0 22px; }}
    #menu a {{ min-height: 44px; padding: 0; }}
    #menu a[aria-current="page"]::after {{ content: none; }}
    html {{ scroll-padding-top: 176px; }}
  }}
</style>
</head>
<body>
<header>
  <div class="wrap bar">
    <div class="title">
      <strong>진행상황</strong>
      <span class="state{"" if tof_connected else " bad"}">{state}</span>
    </div>
    <button id="menu-button" type="button" aria-expanded="false" aria-controls="menu">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
      <span id="menu-label">메뉴</span>
    </button>
  </div>
  <nav id="menu" class="wrap" aria-label="화면">
    <ul>
{nav}
    </ul>
  </nav>
  <nav class="wrap jump" aria-label="섹션 바로가기">
{chips}
  </nav>
</header>
<div id="scrim"></div>
<main class="wrap">
<div class="doc-head">
{intro}
</div>
{now_block}
{blocks}
</main>
<script>
  (() => {{
    const button = document.getElementById('menu-button'), menu = document.getElementById('menu'), scrim = document.getElementById('scrim');
    const label = document.getElementById('menu-label');
    // The open menu (phones) never runs past the visible screen: only the list scrolls, the header bar with the
    // button and the section links stay reachable. Height from the visual viewport (iPhone Safari bars excluded).
    const jump = document.querySelector('.jump');
    function fitMenu() {{
      if (!menu.classList.contains('open') || getComputedStyle(button).display === 'none') {{ menu.style.maxHeight = ''; return; }}
      const vv = window.visualViewport;
      const bottom = (vv ? vv.offsetTop + vv.height : window.innerHeight) - jump.getBoundingClientRect().height - 8;
      menu.style.maxHeight = Math.max(120, Math.floor(bottom - menu.getBoundingClientRect().top)) + 'px';
    }}
    function setOpen(open, focusButton) {{
      button.setAttribute('aria-expanded', String(open));
      label.textContent = open ? '닫기' : '메뉴';
      menu.classList.toggle('open', open);
      scrim.classList.toggle('open', open);
      if (open) {{ menu.scrollTop = 0; fitMenu(); }}
      if (focusButton) button.focus();
    }}
    window.addEventListener('resize', fitMenu);
    if (window.visualViewport) {{ window.visualViewport.addEventListener('resize', fitMenu); window.visualViewport.addEventListener('scroll', fitMenu); }}
    button.addEventListener('click', () => setOpen(button.getAttribute('aria-expanded') !== 'true'));
    menu.addEventListener('click', e => {{ if (e.target.closest('a')) setOpen(false); }});
    scrim.addEventListener('click', () => setOpen(false));
    document.addEventListener('pointerdown', e => {{
      if (button.getAttribute('aria-expanded') === 'true' && !menu.contains(e.target) && !button.contains(e.target)) setOpen(false);
    }});
    document.addEventListener('keydown', e => {{
      if (e.key === 'Escape' && button.getAttribute('aria-expanded') === 'true') setOpen(false, true);
    }});
    // Section jumps land just below the sticky header, whatever its height (status line or screen links wrapping).
    const header = document.querySelector('header');
    const pad = () => {{ document.documentElement.style.scrollPaddingTop = (header.getBoundingClientRect().height + 8) + 'px'; }};
    pad();
    if (window.ResizeObserver) new ResizeObserver(pad).observe(header);
    // a link to a folded entry (#hist-2) opens it
    function showHash() {{
      const t = location.hash && document.getElementById(location.hash.slice(1));
      if (t) {{ if (t.tagName === 'DETAILS') t.open = true; t.scrollIntoView(); }}
    }}
    showHash();
    window.addEventListener('hashchange', showHash);
  }})();
</script>
</body>
</html>
"""


def ws_send(sock: socket.socket, lock: threading.Lock, opcode: int, payload: bytes) -> None:
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    with lock:
        sock.sendall(header)
        sock.sendall(payload)


def ws_recv(rfile) -> tuple[int, bytes] | None:
    head = rfile.read(2)
    if len(head) < 2:
        return None
    opcode, n = head[0] & 0x0F, head[1] & 0x7F
    if n == 126:
        n = struct.unpack("!H", rfile.read(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", rfile.read(8))[0]
    if n > 65536:
        return None
    mask = rfile.read(4) if head[1] & 0x80 else b""
    data = rfile.read(n)
    if mask:
        # numpy unmask (11-01): a 36,864-byte 심 LED frame at 20 fps is too slow byte by byte; same result as the old loop
        data = (np.frombuffer(data, np.uint8) ^ np.resize(np.frombuffer(mask, np.uint8), len(data))).tobytes()
    return opcode, data


HOST_STATS = HostStats()
SIM_FEED = SimFeed()   # 심 LED frames from outside (AI), latest only — see sim_feed.py


class ViewerHandler(BaseHTTPRequestHandler):
    state: SharedState
    beacon: dict = {}   # BLE 비콘 집계, 수신기(receiver)별 마지막 한 개 (POST /api/beacon, 2026-11-18)
    channels: dict[str, InputChannel]
    page_dir: Path
    mapping: MappingStore
    simulator: SimulatorStore | None   # None = --standalone (Pi 실행본): no simulator, no simulated poses
    visual: VisualStore
    pi_publish: pi_bundle.Publish   # 「Pi로 보내기」 (pi_publish.json next to visual.json)

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in TOOL_PAGES and not {"test", "clean", "embed"} & set(parse_qs(parsed.query, keep_blank_values=True)):
            # editing screens always open with their navigation (뒤로 / 메뉴): a bare address goes to its ?test form.
            # Exhibition outputs (/display/…&clean) and the simulator's embedded screens (?embed) are not affected.
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", path + "?" + (parsed.query + "&" if parsed.query else "") + "test")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path in PAGES:
            page = (self.page_dir / PAGES[path]).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif parsed.path == "/status":
            self._json(self.state.status())
        elif parsed.path == "/inputs":
            self._json(self._inputs())
        elif path == "/progress":
            self._progress()
        elif parsed.path == "/testui.js":
            self._testui()
        elif parsed.path == "/mapout.js":
            self._static_js("mapout.js")
        elif parsed.path == "/config":
            try:
                self.state.update_config(parse_qs(parsed.query))
                self._json(self.state.status())
            except (TypeError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif parsed.path == "/ws":
            tof = parse_qs(parsed.query).get("tof", [None])[0]
            key = None
            if tof is not None:
                try:
                    key = stream_key(tof)
                except (ValueError, TypeError):
                    self.send_error(HTTPStatus.BAD_REQUEST, "bad ToF settings")
                    return
            self._websocket(preset=key)
        elif parsed.path == "/ws/monitor":
            self._websocket(monitor=True)
        elif parsed.path == "/ws/mapping":
            self._store_socket(self.mapping)
        elif parsed.path == "/api/mapping":
            self._json(self.mapping.snapshot())
        elif parsed.path == "/ws/simulator" and self.simulator is not None:
            self._store_socket(self.simulator)
        elif parsed.path == "/ws/visual":
            self._store_socket(self.visual)
        elif parsed.path == "/api/visual":
            self._json(self.visual.snapshot())
        elif parsed.path == "/tofrender.js":
            self._static_js("tofrender.js")
        elif parsed.path == "/api/code-version":   # pages reload themselves when a served script/page file changes
            files = [f for f in self.page_dir.iterdir() if f.suffix in (".js", ".html")]
            self._json({"v": max((f.stat().st_mtime_ns for f in files), default=0)})
        elif parsed.path == "/api/odometry":   # 혈 움직임 추정 now (and the simulator's true ToF pose, to compare)
            o = self.state.odometry
            self._json({"on": o is not None, "estimate": o.pose() if o else None,
                        "truth": self.simulator.tof_pose() if self.simulator is not None and self.state.mock else None,
                        "ms": round(o.ms, 2) if o else None, "matched": o.matched if o else 0, "rejected": o.rejected if o else 0})
        elif parsed.path == "/api/simulator":
            # --standalone: no poses at all (an empty answer, so the pages' input poller keeps working)
            self._json(self.simulator.snapshot() if self.simulator is not None else {"devices": {}, "standalone": True})
        elif parsed.path == "/api/pi-bundle/check":
            # 시각화 작업실 "Pi 실행본": what the composition needs on the Pi (pi_bundle.py)
            comp = self.visual.composition(parse_qs(parsed.query).get("id", [""])[0])
            if comp is None:
                self._json({"error": "no such composition"}, HTTPStatus.NOT_FOUND)
            else:
                snap = self.visual.snapshot()
                self._json({"id": comp["id"], "name": comp["name"], "revision": snap["revision"], "saved": snap["saved"],
                            "hash": pi_bundle.composition_hash(comp), "layers": pi_bundle.requirements(comp)})
        elif parsed.path == "/api/pi-bundle":
            self._pi_bundle(parse_qs(parsed.query))
        elif parsed.path == "/api/pi-publish":
            # the 혈 Pi asking (pi_bundle.follow): have = the hash it shows now
            doc = self.pi_publish.ask(parse_qs(parsed.query).get("have", [""])[0], self.client_address[0])
            self._json(doc if doc is not None else {"error": "보낸 화면 없음"}, HTTPStatus.OK if doc is not None else HTTPStatus.NOT_FOUND)
        elif parsed.path == "/api/pi-publish/status":
            self._json(self.pi_publish.status())
        elif parsed.path == "/api/host":
            # server-host CPU / GPU % for the simulator screen (cached, collected every 2 s only while asked for)
            self._json(HOST_STATS.snapshot())
        elif parsed.path == "/api/venue":
            # read-only venue geometry for the 3D check viewer (there is no write endpoint)
            view = self._venue_view()
            self._json(view if view is not None else {"id": "testroom", "available": False, "error": "test room: no venue geometry file"})
        elif parsed.path == "/api/venue3d-static":
            self._venue3d_static()
        elif parsed.path == "/api/sim/status":
            self._json(SIM_FEED.status())
        elif parsed.path == "/api/beacon":   # 수신기별 마지막 비콘 집계 + 받은 지 몇 ms
            now = time.time()
            self._json({"receivers": {k: dict(v["body"], received_ago_ms=int((now - v["at"]) * 1000)) for k, v in ViewerHandler.beacon.items()}})
        elif parsed.path == "/ws/sim/in":
            self._sim_in()
        elif parsed.path == "/ws/sim":
            self._sim_out()
        elif parsed.path == "/simulator.js":
            self._static_js("simulator.js")
        elif parsed.path in ("/static/person_mesh.bin", "/static/person_anim.bin"):
            # person models for /venue3d: the 오블렛 mock-up person (venues/tools/make_person_mesh.py) and the animated
            # walking person (venues/tools/make_person_anim.py, 2026-11-06)
            data = (self.page_dir / "static" / parsed.path.rsplit("/", 1)[1]).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path.startswith("/static/tower-audio/"):
            audio_assets.serve(self, parsed.path)
        elif parsed.path in ("/static/ripples-example.js", "/static/ripples-example.json",
                             "/static/ripples-activate.rgb.gz", "/static/ripples-sleep.rgb.gz"):
            # Fixed allowlist: view-only example assets, no sensor or LED feed writes.
            path = self.page_dir / "static" / parsed.path.rsplit("/", 1)[1]
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            kind = "application/javascript" if path.suffix == ".js" else "application/json" if path.suffix == ".json" else "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path == "/stream.mjpg":
            self._stream()
        elif parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _inputs(self) -> dict[str, object]:
        s = self.state.status()
        tof = {k: s[k] for k in ("connected", "fps", "resolution", "clients", "mode", "error")}
        return {
            "mock": s["mock"],
            # applied: the attach switch stops collection only for the mock sources (simulation); real devices ignore it
            "hyeol": dict(self.simulator.hyeol_state(), applied=bool(s["mock"])) if self.simulator is not None else None,
            "tof": dict(tof, collecting=s["collecting"]),
            "channels": {name: channel.snapshot() for name, channel in self.channels.items()},
        }

    def _pi_saved(self, cid: str, rev: str) -> tuple[dict, int] | None:
        """The composition exactly as the page showed it: rev = the visual revision the page last saw; a newer one
        (someone changed something since) or a save still pending is refused (409), so the Pi gets what was shown."""
        snap = self.visual.snapshot()
        comp = self.visual.composition(cid)
        if comp is None:
            self._json({"error": "no such composition"}, HTTPStatus.NOT_FOUND)
            return None
        if not snap["saved"] or rev != str(snap["revision"]):
            self._json({"error": "저장이 끝나지 않았거나 다른 화면에서 바뀜 · 다시 시도", "revision": snap["revision"], "saved": snap["saved"]},
                       HTTPStatus.CONFLICT)
            return None
        return comp, snap["revision"]

    def _pi_follow_url(self) -> str:
        """The address the Pi asks for 「Pi로 보내기」: the one this page was opened with, unless that is this machine itself."""
        host = (self.headers.get("Host") or "").strip()
        name = host.rsplit(":", 1)[0].strip("[]")
        return pi_bundle.DEFAULT_FOLLOW if not host or name in ("localhost", "127.0.0.1", "::1") else f"http://{host}"

    def _pi_publish(self) -> None:
        """「Pi로 보내기」: the composition (saved, as shown: same check as the Pi 실행본) becomes what the 혈 Pi follows."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        except (ValueError, TypeError):
            self._json({"error": "bad JSON"}, HTTPStatus.BAD_REQUEST)
            return
        got = self._pi_saved(str(body.get("id", "")), str(body.get("rev", "")))
        if got is None:
            return
        try:
            self._json(self.pi_publish.publish(*got))
        except OSError as exc:
            self._json({"error": f"저장 실패: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _pi_bundle(self, query: dict) -> None:
        """The Pi 실행본 zip of one saved composition (pi_bundle.py), as shown (_pi_saved), following this server."""
        got = self._pi_saved(query.get("id", [""])[0], query.get("rev", [""])[0])
        if got is None:
            return
        comp, revision = got
        data, name, manifest = pi_bundle.build(self.page_dir, comp, revision, self._pi_follow_url())
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f"attachment; filename=\"pi-bundle.zip\"; filename*=UTF-8''{quote(name)}")
        self.send_header("X-Bundle-Revision", str(manifest["revision"]))
        self.send_header("X-Bundle-Hash", manifest["hash"])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _venue_view(self) -> dict | None:
        venue = self.simulator.venue if self.simulator is not None else None
        if venue is None:
            return None
        return dict(venue.view(), available=True, sensors={
            "tof_height_mm": SIM_TOF_HEIGHT, "tof_pitch_deg": SIM_TOF_PITCH, "tof_hfov_deg": HFOV_DEG,
            "tof_size": [TOF_W, TOF_H], "tof_range_mm": MAX_RANGE_MM, "distance_height_mm": SIM_DISTANCE_HEIGHT})

    def _venue3d_static(self) -> None:
        """/venue3d 「웹판 내려받기」: the static web snapshot (tools/export_static_venue3d.py) of the current layout as a
        zip with 읽어주세요.txt for whoever puts it on a site (user 2026-11-09). Reads only; nothing is saved."""
        view = self._venue_view()
        if view is None:
            self._json({"error": "공간 도면이 없는 테스트 방"}, HTTPStatus.NOT_FOUND)
            return
        from tools import export_static_venue3d as E
        files = E.build(view, self.simulator.snapshot(), self.simulator.venue.default_layout())
        stamp = time.strftime("%Y%m%d-%H%M")
        data = E.zip_bytes(files, f"ripples-{view['id']}-web-{stamp}")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f"attachment; filename=\"ripples-{view['id']}-web-{stamp}.zip\"")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _testui(self) -> None:
        # Shared test UI script; the screen menu uses the same NAV_LINKS as /progress.
        script = f"const RIPPLES_NAV = {json.dumps(NAV_LINKS, ensure_ascii=False)};\n".encode("utf-8")
        script += (self.page_dir / "testui.js").read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(script)))
        self.end_headers()
        self.wfile.write(script)

    def _static_js(self, name: str) -> None:
        script = (self.page_dir / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(script)))
        self.end_headers()
        self.wfile.write(script)

    def _progress(self) -> None:
        # PROGRESS.md is read on every request, so edits show up on reload without restarting the server.
        status = self.state.status()
        page = progress_page(self.page_dir / "PROGRESS.md", self.state.mock, bool(status["connected"]), self.state.venue_name).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def _maek_post(self) -> None:
        """맥 Pi live values (see maek-teensy-speaker/docs/SIM_FORMAT.md). Fills the same channels the mock used:
        maek_mic = [left, center, right] 0–1, maek_features = direction / haptic / raw levels."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("body size must be 1–4096 bytes")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict) or body.get("v") != 1:
                raise ValueError("expected a JSON object with v: 1")
            mics = body.get("mics")
            if not (isinstance(mics, list) and len(mics) == 3
                    and all(isinstance(x, (int, float)) and 0 <= x <= 1 for x in mics)):
                raise ValueError("mics must be three numbers 0–1 (left, center, right)")
            haptic = body.get("haptic") or [0, 0]
            direction = body.get("direction_deg")
            source = "maek-pi:" + str(body.get("device", "mic"))[:32]
            self.channels["maek_mic"].update_real([round(float(x), 3) for x in mics], source)
            if "maek_features" in self.channels:
                self.channels["maek_features"].update_real({
                    "pitch_hz": None, "pitch_source": "none",
                    "direction_deg": round(float(direction), 1) if isinstance(direction, (int, float)) else None,
                    "direction_source": "maek-pi" if isinstance(direction, (int, float)) else "none",
                    "direction_contrast": body.get("level_bias"),
                    "haptic": [round(float(x), 3) for x in haptic][:2],
                    "rms": body.get("rms"), "seq": body.get("seq"),
                }, source)
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (ValueError, UnicodeDecodeError, TypeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _beacon_post(self) -> None:
        """혈 · 맥 Pi 의 BLE 비콘 1초 집계 (work/2026-11-18_ble_beacon/beacon/README.md). 수신기별 마지막 것만 둔다."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("body size must be 1–16384 bytes")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict) or body.get("v") != 1 or body.get("type") != "beacon":
                raise ValueError("expected a JSON object with v: 1, type: beacon")
            if not isinstance(body.get("nodes"), dict):
                raise ValueError("nodes must be an object")
            receiver = str(body.get("receiver") or self.client_address[0])[:48]
            ViewerHandler.beacon[receiver] = {"body": body, "at": time.time()}
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (ValueError, UnicodeDecodeError, TypeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _low_latency_socket(self) -> socket.socket:
        sock = self.connection
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32 * 1024)
        return sock

    def _ws_handshake(self) -> bool:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            self.send_error(HTTPStatus.BAD_REQUEST)
            return False
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.protocol_version = "HTTP/1.1"
        self.close_connection = True
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        return True

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/sim/frame":
            # one 심 LED frame: raw bytes of uint8 (12, 16, 64, 3)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length != SIM_FRAME_BYTES:
                    raise ValueError(f"body must be {SIM_FRAME_BYTES} bytes (uint8 12×16×64×3), got {length}")
                SIM_FEED.push(self.rfile.read(length), "http")
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/pi-publish":
            self._pi_publish()
            return
        if parsed.path == "/api/maek":
            self._maek_post()
            return
        if parsed.path == "/api/beacon":
            self._beacon_post()
            return
        store = {"/api/mapping": self.mapping, "/api/simulator": self.simulator, "/api/visual": self.visual}.get(parsed.path)
        if store is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("body size must be 1–65536 bytes")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            snapshot = store.apply(body.get("op"), final=body.get("final", True) is not False, origin="http")
            self._json(snapshot)
        except (ValueError, UnicodeDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    # Store sync (/ws/mapping, /ws/simulator): every page keeps one socket per store. The server sends the latest
    # state on connect and after each change (latest only, no queue); pages send
    # {"type": "op", "op": {...}, "final": bool, ...} (op kinds in mapping_store / simulator_store).
    def _store_socket(self, store) -> None:
        if not self._ws_handshake():
            return
        sock = self._low_latency_socket()
        lock = threading.Lock()
        closed = threading.Event()

        def receive() -> None:
            try:
                while True:
                    message = ws_recv(self.rfile)
                    if message is None or message[0] == 0x8:
                        break
                    opcode, data = message
                    if opcode == 0x9:
                        ws_send(sock, lock, 0xA, data)
                        continue
                    if opcode != 0x1:
                        continue
                    try:
                        msg = json.loads(data.decode("utf-8"))
                        if not isinstance(msg, dict) or msg.get("type") != "op":
                            raise ValueError("unknown message")
                        seq = msg.get("seq")
                        store.apply(msg.get("op"), final=bool(msg.get("final")),
                                    origin=str(msg.get("client"))[:64], seq=seq if isinstance(seq, int) else None)
                    except (ValueError, UnicodeDecodeError) as exc:
                        ws_send(sock, lock, 0x1, json.dumps({"type": "error", "error": str(exc)}).encode())
            except (OSError, ValueError, struct.error):
                pass
            finally:
                closed.set()

        threading.Thread(target=receive, daemon=True).start()
        last = None
        try:
            while not self.state.stop.is_set() and not closed.is_set():
                version = store.wait_change(last if last is not None else -1, timeout=0.5)
                if version == last:
                    continue
                last = version
                ws_send(sock, lock, 0x1, json.dumps(store.snapshot()).encode())
            if not closed.is_set():
                ws_send(sock, lock, 0x8, b"")
        except OSError:
            pass

    # 심 LED frames: /ws/sim/in takes binary frames from the AI (36,864 bytes each), /ws/sim sends the latest to /sim pages.
    def _sim_in(self) -> None:
        if not self._ws_handshake():
            return
        sock = self._low_latency_socket()
        lock = threading.Lock()
        try:
            while not self.state.stop.is_set():
                message = ws_recv(self.rfile)
                if message is None or message[0] == 0x8:
                    break
                opcode, data = message
                if opcode == 0x9:
                    ws_send(sock, lock, 0xA, data)
                elif opcode == 0x2:
                    try:
                        SIM_FEED.push(data, "ws")
                    except ValueError as exc:
                        ws_send(sock, lock, 0x1, json.dumps({"type": "error", "error": str(exc)}).encode())
        except (OSError, ValueError, struct.error):
            pass

    def _sim_out(self) -> None:
        if not self._ws_handshake():
            return
        sock = self._low_latency_socket()
        lock = threading.Lock()
        closed = threading.Event()

        def receive() -> None:
            try:
                while True:
                    message = ws_recv(self.rfile)
                    if message is None or message[0] == 0x8:
                        break
            except (OSError, ValueError, struct.error):
                pass
            finally:
                closed.set()

        threading.Thread(target=receive, daemon=True).start()
        last = -1
        try:
            while not self.state.stop.is_set() and not closed.is_set():
                version, frame = SIM_FEED.wait(last, timeout=0.5)
                if version == last or frame is None:
                    continue
                last = version
                ws_send(sock, lock, 0x2, frame)   # latest only: frames that arrive while this one is sent are skipped
            if not closed.is_set():
                ws_send(sock, lock, 0x8, b"")
        except OSError:
            pass

    def _websocket(self, monitor: bool = False, preset: str | None = None) -> None:
        if not self._ws_handshake():
            return
        if preset is not None:
            self.state.subscribe_preset(preset, +1)

        sock = self._low_latency_socket()
        lock = threading.Lock()
        wanted = threading.Event()
        closed = threading.Event()

        # The browser sends "next" after receiving a frame, so at most one frame is in flight.
        def receive() -> None:
            try:
                while True:
                    message = ws_recv(self.rfile)
                    if message is None or message[0] == 0x8:
                        break
                    opcode, data = message
                    if opcode == 0x9:
                        ws_send(sock, lock, 0xA, data)
                    elif data == b"next":
                        wanted.set()
                    elif data.startswith(b"ping "):
                        reply = b"pong " + data[5:] + f" {time.time() * 1000:.3f}".encode()
                        ws_send(sock, lock, 0x1, reply)
            except (OSError, ValueError, struct.error):
                pass
            finally:
                closed.set()
                wanted.set()

        threading.Thread(target=receive, daemon=True).start()
        with self.state.condition:
            self.state.clients += 1
        last_id = -1
        try:
            while not self.state.stop.is_set() and not closed.is_set():
                if not wanted.wait(timeout=0.5) or closed.is_set():
                    continue
                frame_id, _, packet = self.state.wait_frame(last_id, timeout=0.5, monitor=monitor, preset=preset)
                if frame_id == last_id or packet is None:
                    continue
                wanted.clear()
                last_id = frame_id
                ws_send(sock, lock, 0x2, packet)
            if not closed.is_set():
                ws_send(sock, lock, 0x8, b"")
        except OSError:
            pass
        finally:
            with self.state.condition:
                self.state.clients -= 1
            if preset is not None:
                self.state.subscribe_preset(preset, -1)

    def _stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._low_latency_socket()
        last_id = -1
        try:
            while not self.state.stop.is_set():
                frame_id, frame, _ = self.state.wait_frame(last_id, timeout=2.0)
                if frame_id == last_id or frame is None:
                    continue
                last_id = frame_id
                jpeg = self.state.jpeg_for(frame_id, frame)
                if jpeg is None:
                    continue
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                    + jpeg
                    + b"\r\n"
                )
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mock", action="store_true", help="show generated test data (ToF and all other inputs)")
    parser.add_argument("--venue", default="testroom", choices=("testroom",) + VENUES,
                        help="simulator geometry, fixed for this run (venues/<id>/geometry.json); "
                             "testroom = the procedural test room (fallback / test fixture, default)")
    parser.add_argument("--simulator-file", default="data/simulator.json",
                        help="simulator poses JSON, relative to this folder unless absolute (default data/simulator.json)")
    parser.add_argument("--mapping-file", default="data/mapping.json",
                        help="mapping corners JSON, relative to this folder unless absolute (default data/mapping.json)")
    parser.add_argument("--visual-file", default=None,
                        help="visual presets + output slots JSON (default: visual.json next to the mapping file)")
    parser.add_argument("--legacy-mapping-file", default=None,
                        help="if --mapping-file does not exist yet, start it as a copy of this file (the old shared "
                             "data/mapping.json); the old file is left as it is")
    parser.add_argument("--standalone", action="store_true",
                        help="Pi 실행본: no simulator (no simulated 혈 / 맥 poses, people or attach switch); the real ToF only")
    parser.add_argument("--follow", default="",
                        help="Pi 실행본: the exhibition server URL whose 「Pi로 보내기」 this server follows (pi_bundle.follow)")
    parser.add_argument("--camera-index", type=int, default=0,
                        help="Arducam CSI device index (camera.open(Connection.CSI, n)); default 0")
    parser.add_argument("--no-odometry", action="store_true",
                        help="do not estimate 혈's movement from the ToF depth (odometry.py; on by default)")
    parser.add_argument("--source", default="direct", choices=("direct", "collector"),
                        help="ToF 깊이를 어디서: direct = 카메라 직접, collector = 수집단 소켓 (run/collector.sock) · 환경변수 COLLECTOR_LISTEN=9500 이면 혈 Pi 송신을 TCP 로 받음")
    parser.add_argument("--distance", default="", choices=("", "tofsense", "collector"),
                        help="천장 거리 센서 receiver: tofsense = Nooploop TOFSense-F2 P on a serial port (tofsense.py); "
                             "default none (the mock feeds it with --mock)")
    parser.add_argument("--distance-port", default="auto",
                        help="serial port of the distance sensor: auto (USB adapter, then the GPIO UART) or a path")
    parser.add_argument("--distance-baud", type=int, default=0, help="its speed; 0 = try 921600 (factory), 115200, …")
    args = parser.parse_args()

    state = SharedState()
    state.mock = args.mock
    channels = create_channels()
    ViewerHandler.state = state
    ViewerHandler.channels = channels
    ViewerHandler.page_dir = Path(__file__).resolve().parent
    here = lambda p: Path(p) if Path(p).is_absolute() else ViewerHandler.page_dir / p
    venue = load_venue(args.venue)
    state.venue_name = venue.name if venue is not None else "test room"
    mapping_path = here(args.mapping_file)
    if args.legacy_mapping_file and not mapping_path.exists() and here(args.legacy_mapping_file).exists():
        # one-time migration: the corners made before per-venue files keep working (the old file stays untouched)
        mapping_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(here(args.legacy_mapping_file), mapping_path)
        print(f"Mapping: started {args.mapping_file} from {args.legacy_mapping_file}", flush=True)
    mapping = MappingStore(mapping_path)
    ViewerHandler.mapping = mapping
    simulator = None if args.standalone else SimulatorStore(here(args.simulator_file), venue)
    ViewerHandler.simulator = simulator
    visual = VisualStore(here(args.visual_file) if args.visual_file else mapping_path.with_name("visual.json"))
    ViewerHandler.visual = visual
    ViewerHandler.pi_publish = pi_bundle.Publish(visual.path.with_name("pi_publish.json"))
    if args.follow:
        pi_bundle.follow(args.follow, visual, ViewerHandler.page_dir.parent / "manifest.json", state.stop)
        print(f"Pi로 보내기: {args.follow} 를 따라감", flush=True)
    print(f"Venue: {args.venue} ({state.venue_name}) · simulator {'none (standalone)' if simulator is None else args.simulator_file}"
          f" · mapping {args.mapping_file}", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), ViewerHandler)
    worker = threading.Thread(target=camera_loop, args=(state, args.mock, simulator, venue, visual, args.camera_index,
                                                        not args.no_odometry, args.source), daemon=True)
    worker.start()
    if args.distance == "collector":   # 천장 거리도 수집단(또는 혈 Pi 송신)에서 받음
        import collector_client
        collector_client.start_distance(channels["distance"], state.stop)
        print("천장 거리 센서: collector 에서 받음", flush=True)
    elif args.distance == "tofsense":   # real 천장 거리 센서 (Pi): imported only here (termios: not on Windows)
        import tofsense
        tofsense.start(channels["distance"], state.stop, args.distance_port, args.distance_baud or None)
        print(f"천장 거리 센서: TOFSense-F2 P · 포트 {args.distance_port}", flush=True)
    elif args.mock and simulator is not None:
        # No real receivers exist yet for the distance sensor, audio or AI/LED; without --mock they stay "none".
        # the 20 m distance mock reads the same 혈 pose: straight up from 2.0 m to the venue's ceiling (None when the
        # venue's ceiling is unknown: no reading)
        start_mock_sources(channels, state.stop, simulator.up_distance,
                           lambda: None if simulator.hyeol_state()["collecting"] else "혈 해제 (아이들링) · 수집 멈춤",
                           maek_pose=lambda: simulator.pose("maek"), people=simulator.people_for_tof)

    print(f"ToF viewer: http://ripples-tof.local:{args.port}", flush=True)
    print(f"연결 관리:  http://localhost:{args.port}/operator  (blind outputs: /display/blind?space=a&clean, /display/blind?space=b&clean)", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        state.stop.set()
        with state.condition:
            state.condition.notify_all()
        mapping.stop()
        if simulator is not None:
            simulator.stop()
        visual.stop()
        server.server_close()
        worker.join(timeout=3)
        print("Stopped", flush=True)


if __name__ == "__main__":
    main()
