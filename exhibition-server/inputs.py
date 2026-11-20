"""Input channels other than the ToF camera (20m distance sensor, audio, AI/LED).

Each channel keeps only its latest value (no queue). A source thread calls channel.update(value);
screens read channel.snapshot() through /inputs. Only mock sources exist for now. A real receiver
later replaces the mock source and calls the same update(), so the screens do not change.
The value shape of the real devices (protocol, units, LED grid format) is not decided yet.
"""

from __future__ import annotations

import math
import random
import threading
import time
from typing import Callable


STALE_SECONDS = 3.0


class InputChannel:
    def __init__(self, label: str, unit: str = "") -> None:
        self.label = label
        self.unit = unit
        self.source = "none"  # "mock" now; a real receiver sets its own name
        self._lock = threading.Lock()
        self._value: object = None
        self._seq = 0
        self._updated = 0.0
        self._error: str | None = None
        self._rate = 0.0
        self._count = 0
        self._since = time.monotonic()
        self._paused: str | None = None  # why collection is stopped (e.g. 혈 detached / idling), else None
        self.real_until = 0.0  # while a real device keeps sending, mock sources stay quiet

    def update(self, value: object) -> None:
        with self._lock:
            now = time.monotonic()
            if not self._seq:
                self._since = now
            self._value = value
            self._seq += 1
            self._updated = now
            self._error = None
            self._count += 1
            if now - self._since >= 1.0:
                self._rate = self._count / (now - self._since)
                self._count = 0
                self._since = now

    def update_real(self, value: object, source: str) -> None:
        """A value from a real device (e.g. POST /api/maek). Mock sources pause while these keep arriving."""
        self.source = source
        self.real_until = time.monotonic() + STALE_SECONDS
        self.pause(None)
        self.update(value)

    def pause(self, reason: str | None) -> None:
        """Mark collection as stopped (no new values) with a reason, or resume with None."""
        with self._lock:
            self._paused = reason

    def fail(self, message: str) -> None:
        with self._lock:
            self._error = message

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            age = time.monotonic() - self._updated if self._seq else None
            fresh = age is not None and age < STALE_SECONDS and self._paused is None  # paused: no current value
            return {
                "label": self.label,
                "unit": self.unit,
                "source": self.source,
                "connected": fresh and self._error is None,
                "value": self._value if fresh else None,
                "seq": self._seq,
                "age_s": round(age, 2) if age is not None else None,
                "rate_hz": round(self._rate, 1) if fresh else 0.0,
                "error": self._error,
                "paused": self._paused,
            }


def create_channels() -> dict[str, InputChannel]:
    return {
        "distance": InputChannel("20m 거리 센서", "m"),
        "audio": InputChannel("audio", "level 0–1"),
        "ai_led": InputChannel("AI / LED"),
        # 맥: three microphones close together on the device; one sample = [mic 1, mic 2, mic 3] taken at the same time
        "maek_mic": InputChannel("맥 마이크 3개 (가정 배치)", "level 0–1"),
        "maek_features": InputChannel("맥 합성 소리 특성 (시연)", "pitch Hz / rough direction °"),
    }


class MockSource(threading.Thread):
    def __init__(self, channel: InputChannel, hz: float, generate: Callable[[float], object], stop: threading.Event,
                 paused: Callable[[], str | None] | None = None) -> None:
        super().__init__(daemon=True)
        self.channel, self.hz, self.generate, self.stop = channel, hz, generate, stop
        self.paused = paused  # returns a reason while no values should be collected

    def run(self) -> None:
        self.channel.source = "mock"
        started = next_tick = time.monotonic()
        while not self.stop.is_set():
            next_tick += 1 / self.hz
            self.stop.wait(max(0.0, next_tick - time.monotonic()))
            next_tick = max(next_tick, time.monotonic() - 1 / self.hz)
            if time.monotonic() < self.channel.real_until:
                continue  # a real device is sending; do not overwrite it with generated values
            self.channel.source = "mock"
            reason = self.paused() if self.paused else None
            self.channel.pause(reason)
            if reason is None:
                self.channel.update(self.generate(time.monotonic() - started))


def mock_distance(t: float) -> float:
    # Someone walking toward and away from the sensor between about 1.5 m and 18 m.
    return round(1.5 + 8.25 * (1 - math.cos(t * 0.25)) + random.gauss(0, 0.03), 2)


def mock_audio(t: float) -> float:
    # Slow swell plus a short decaying burst every 3 s, so changes over time are easy to see.
    swell = 0.1 + 0.3 * (0.5 + 0.5 * math.sin(t * 0.8))
    burst = 0.5 * math.exp(-(t % 3.0) * 4)
    return round(min(1.0, max(0.0, swell + burst + random.uniform(-0.03, 0.03))), 3)


def mock_ai_led(t: float) -> None:
    return None  # heartbeat only; the AI state / LED grid format is not decided yet


# --- 맥 microphones (mock, 2026-10-29) ---------------------------------------------------------------------------------
# The user confirmed 3 microphones close together on 맥. Everything below that is not that fact is an ASSUMPTION for the
# mock only (real model, spacing, mounting, sync and data format are undecided): the mics sit on a circle of MIC_RADIUS_MM
# around the 맥 pose at MIC_HEIGHT_MM, mic 1 facing the device heading, 2 and 3 at +120° / +240°. Sound sources = the
# simulated visitors (each talks in random bursts from mouth height) over a room noise floor; each mic gets the summed
# power with inverse-square distance (free field, no walls / reflections / delays), plus its own small noise. Level 0–1
# = 30–90 dB SPL. Close sources give clearly different levels on the three mics, far ones almost the same.
MIC_RADIUS_MM = 100.0          # assumption: equilateral triangle, about 17 cm between mics
MIC_HEIGHT_MM = 1200.0         # assumption
MOUTH_HEIGHT_MM = 1550.0
TALK_DB_AT_1M = 62.0           # conversational speech at 1 m
FLOOR_DB = 36.0                # quiet gallery
LEVEL_DB = (30.0, 90.0)        # level 0 → 1


def mic_positions(pose: dict) -> list[tuple[float, float]]:
    out = []
    for k in range(3):
        a = math.radians(pose["heading"] + 120 * k)   # heading 0 = +y, 90 = +x (simulator convention)
        out.append((pose["x"] + MIC_RADIUS_MM * math.sin(a), pose["y"] + MIC_RADIUS_MM * math.cos(a)))
    return out


def make_maek_mics(pose: Callable[[], dict], people: Callable[[], list[dict]],
                   features: InputChannel | None = None) -> Callable[[float], list[float]]:
    talk: dict[int, dict] = {}   # per visitor: on/off state, time left, loudness offset

    def source_db(i: int, t: float, dt: float) -> float | None:
        s = talk.setdefault(i, {"on": False, "left": random.uniform(0.5, 4.0), "gain": random.uniform(-4, 4)})
        s["left"] -= dt
        if s["left"] <= 0:
            s["on"] = not s["on"]
            s["left"] = random.uniform(0.6, 3.0) if s["on"] else random.uniform(1.0, 6.0)
        if not s["on"]:
            return None
        syllables = 0.5 + 0.5 * math.sin(2 * math.pi * 4.0 * t + i)   # ~4 syllables / s
        return TALK_DB_AT_1M + s["gain"] - 8.0 * (1 - syllables)

    last = [0.0]

    def generate(t: float) -> list[float]:
        dt = t - last[0] if last[0] else 0.0
        last[0] = t
        mics = mic_positions(pose())
        srcs = []
        for i, p in enumerate(people()):
            db = source_db(i, t, dt)
            if db is not None:
                srcs.append((p["x"], p["y"], db, i))
        if not srcs and not people():   # no visitors in this venue: one virtual talker circling 1.5 m away
            a = t * 0.4
            pz = pose()
            srcs.append((pz["x"] + 1500 * math.sin(a), pz["y"] + 1500 * math.cos(a), TALK_DB_AT_1M - 4 + 4 * math.sin(t * 6), -1))
        out = []
        for mx, my in mics:
            power = 10 ** (FLOOR_DB / 10)
            for sx, sy, db, _ in srcs:
                d2 = ((sx - mx) ** 2 + (sy - my) ** 2 + (MOUTH_HEIGHT_MM - MIC_HEIGHT_MM) ** 2) / 1e6   # m²
                power += 10 ** (db / 10) / max(d2, 0.01)
            spl = 10 * math.log10(power) + random.gauss(0, 0.4)   # each mic its own small noise
            out.append(round(min(1.0, max(0.0, (spl - LEVEL_DB[0]) / (LEVEL_DB[1] - LEVEL_DB[0]))), 3))
        if features is not None:
            features.source = "mock"
            # Direction uses only the three simulated levels. It is a rough loudest-side estimate, not time-delay
            # localisation; nearly equal levels have no usable direction. Pitch is a synthetic source attribute,
            # not something the level-only microphones can measure.
            contrast = max(out) - min(out)
            relative = [v - sum(out) / 3 for v in out]
            heading = pose()["heading"]
            vx = sum(v * math.sin(math.radians(heading + 120 * k)) for k, v in enumerate(relative))
            vy = sum(v * math.cos(math.radians(heading + 120 * k)) for k, v in enumerate(relative))
            direction = (math.degrees(math.atan2(vx, vy)) + 360) % 360 if contrast >= 0.015 else None
            centre = pose()
            dominant = max(srcs, key=lambda s: 10 ** (s[2] / 10) /
                           max((((s[0] - centre["x"]) ** 2 + (s[1] - centre["y"]) ** 2) / 1e6), 0.01), default=None)
            pitch = round(180 + 55 * math.sin(t * 1.1 + dominant[3] * 1.7), 1) if dominant else None
            features.update({"pitch_hz": pitch, "direction_deg": round(direction, 1) if direction is not None else None,
                             "direction_contrast": round(contrast, 3), "pitch_source": "synthetic", "direction_source": "level_difference"})
        return out

    return generate


def start_mock_sources(channels: dict[str, InputChannel], stop: threading.Event,
                       distance_mm: Callable[[], float | None] | None = None,
                       distance_paused: Callable[[], str | None] | None = None,
                       maek_pose: Callable[[], dict] | None = None, people: Callable[[], list[dict]] | None = None) -> None:
    # distance_mm: the 혈 device's straight-up distance from the simulator pose (server.py); None from it means no
    # reading (the venue's ceiling is unknown). Without it the old walking-person sweep (mock_distance) is used.
    if distance_mm is None:
        MockSource(channels["distance"], 10, mock_distance, stop).start()
    else:
        def up(t: float) -> float | None:
            mm = distance_mm()
            return None if mm is None else round(mm / 1000 + random.gauss(0, 0.005), 3)
        MockSource(channels["distance"], 10, up, stop, distance_paused).start()
    MockSource(channels["audio"], 30, mock_audio, stop).start()
    MockSource(channels["ai_led"], 1, mock_ai_led, stop).start()
    if "maek_mic" in channels:   # 맥 pose / visitors from the simulator (without it: a still device at the origin, nobody)
        MockSource(channels["maek_mic"], 30, make_maek_mics(maek_pose or (lambda: {"x": 0.0, "y": 0.0, "heading": 0.0}),
                                                            people or (lambda: []), channels.get("maek_features")), stop).start()
