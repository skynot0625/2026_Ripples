"""Server-authoritative simulator poses for the two moving devices, driven by hold-to-move buttons.

    data/<venue>/simulator.json (path from the exhibition settings / --simulator-file)
    {"version": 2, "venue": "m30", "revision": 3, "updated_at": "…", "hyeol_attached": true,
     "devices": {"hyeol": {"x": 3450.0, "y": 2000.0, "heading": 0.0},
                 "maek":  {"x": 7950.0, "y": 2000.0, "heading": 0.0}}}

- The venue is fixed for the whole run (--venue; venue.py). "testroom" = the procedural mock's simplified test room
  (mock_tof.ROOM), kept as fallback / test fixture; it is not a real venue.
- 2D pose only: x, y on the plan (mm), heading in degrees, 0 = facing +y, 90 = +x.
- 혈 (hyeol): ToF camera and 20 m distance sensor share this one pose; their heights (1.5 m / 2.0 m), the ToF facing
  forward and the distance sensor pointing straight up are fixed (mock_tof.SIM_*), not stored or adjustable.
- 맥 (maek): the microphone device, a separate pose.
- 혈 attached / detached ({"op": "attach", "device": "hyeol", "attached": bool}): attached = in use (사용 중), the ToF and
  20 m distance mocks keep producing values; detached = idling (아이들링), no new sensor values are collected
  (server.py, inputs.py). This is a simulation switch only: how the real device will tell that it is attached is not
  decided yet. Saved with the poses and shared with every page like them.
- Driving: pages send {"op": "drive", "device", "move": -1|0|1, "turn": -1|0|1, "strafe": -1|0|1} on press / release and every
  DRIVE_REPEAT_S while a button is held. The server integrates the motion (MOTION: fixed speeds with gentle
  acceleration / deceleration), so every page sees the same movement. An input that is not repeated within
  MOTION["input_timeout_s"] counts as released (a page that disappears mid-press does not leave a device running).
  "strafe" moves sideways relative to the heading (right = +1) without turning; it accelerates / decelerates like
  forward / backward. A value between -1 and 1 (the 작업실 joystick, 2026-10-15) asks for that fraction of the top
  speed; acceleration, deceleration, collisions and the timeout are the same as for the buttons.
- Collision (venue.Venue.blocked): a move that would hit a wall / column / glass or leave the walkable area goes as far
  as the contact point, then the rest of the step slides along the surface (the part pointing into it is dropped).
  When almost nothing is left to slide (meeting the surface within MOTION["slide_min_angle_deg"] of head-on) or the
  slide runs straight into another surface (a corner), the device stops (speed 0). Turning is always allowed.
- Mock-up people (venue layer "people", e.g. venues/m30/mockup.json): a few people wander slowly inside their own
  areas, keeping clear of walls, the central structure, blinds (also the parts above head height), the devices and each
  other; they turn and speed up / slow down gradually (no jumps). A person who is on or near the path a device is
  driving along steps out of the way early (same walking speed / acceleration / turning; may leave its own area to do
  so). Devices still cannot drive through a person: only when a person has no free space to step to does the device
  stop at the person (reported as blocked). Positions and the simple silhouette (PERSON_PARTS) are one shared server
  state (every snapshot; the ToF mock traces the same parts, /venue3d draws them) and are not saved.
- Saving follows mapping_store: the resting pose is written at once, poses while moving debounced (0.5 s); temp file
  + fsync, previous file kept as .bak, os.replace; a damaged file is copied aside and the .bak (or start poses) is
  used. A file saved for another venue is copied aside and not used.
"""

from __future__ import annotations

import copy
import json
import math
import os
import random
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from mock_tof import ROOM, SIM_DISTANCE_HEIGHT, vertical_distance_mm

DEVICES = ("hyeol", "maek")
DEVICE_NAMES = {"hyeol": "혈", "maek": "맥"}
from mock_tof import SIM_TOF_BACK_MM as SIM_TOF_BACK, SIM_TOF_HEIGHT as SIM_TOF_H   # noqa: E402  (ToF on 혈, 2026-11-06)
TESTROOM_START = {"hyeol": {"x": 0.0, "y": 0.0, "heading": 0.0}, "maek": {"x": -1000.0, "y": 2400.0, "heading": 180.0}}
TESTROOM_MARGIN_MM = 250.0  # test room only: keep the device this far from the walls
DEBOUNCE_SECONDS = 0.5

# Motion of a device pushed / pulled by a person. Initial, conservative experiment values — the real speeds are not
# decided yet (README "simulator"). This is the one place to change them; pages have no speed controls.
MOTION = {
    "forward_mm_s": 500.0,       # top speed forward (0.5 m/s, slow walk)
    "reverse_mm_s": 300.0,       # top speed backward
    "accel_mm_s2": 500.0,        # 0 → top speed in about 1 s
    "decel_mm_s2": 1000.0,       # top speed → 0 in about 0.5 s after release (about 12 cm of coasting)
    "turn_deg_s": 30.0,          # top turning speed
    "turn_accel_deg_s2": 60.0,   # 0 → top turning speed in about 0.5 s
    "turn_decel_deg_s2": 120.0,  # stops turning in about 0.25 s after release
    "strafe_mm_s": 300.0,        # top speed sideways (좌이동 / 우이동); same acceleration / deceleration as forward
    "slide_min_angle_deg": 10.0, # meeting a wall closer to head-on than this stops the device instead of sliding
    "tick_hz": 30.0,             # server integration rate while a device moves
    "input_timeout_s": 0.6,      # a held button must be repeated within this time (pages repeat every 0.2 s)
}
DRIVE_REPEAT_S = 0.2
# 모드 (2026-11-06): 수집 = sessions at random times
MODE = {
    "first_s": (3.0, 20.0),          # first session after 수집 starts
    "between_s": (3.0, 12.0),        # parked between sessions (user: used too rarely)
    "hands_off_s": 20.0,             # after driving it by hand, no automatic session for this long
}
# Taped no-go areas on the floor (/venue3d tape; people and devices keep out, user 2026-11-06)
TAPE = {
    "sim_mm": 450.0,                 # hexagon this far outside 심's plate
    "pc_mm": 350.0,                  # around the PC
    "cable_mm": 250.0,               # each side of the cable
    "cable_box": (700.0, 650.0, 10380.0, 19500.0),   # wall faces for the cable bend side (as /venue3d)
}
# Visits of the mock-up people (2026-11-06): in through the lobby door (the vestibule's inner door) and the empty
# lobby window bays, stand and look at 심 / 혈 / 맥 (now and then just around), out again, someone else comes in later.
VISIT = {
    "door": (-3870.0, 250.0),        # inside the lobby, in front of the vestibule's inner door
    "looks": (2, 3),                 # looks per visit
    "weights": (5, 3, 3, 0),         # 심, 혈, 맥, around (user: no looking around — stand and look at a work)
    "weights_sim": (12, 1, 1, 0),    # 학습 / 재현: mostly round 심
    "keep_door_mm": 2500.0,          # no looking spot this near the entrance
    "look_s": (5.0, 10.0),           # standing looking, still
    "count": 6,                      # visitors at most (the venue's 3 + more coming in through the door, user)
    "sim_mm": (2000.0, 2800.0),      # standing this far from 심's centre
    "device_mm": (1500.0, 2100.0),   # … from a device's centre
    "moving_mm": (2800.0, 3600.0),   # … from a device that is walking around
    "away_s": (4.0, 14.0),           # outside before the next visitor comes in
}
# 관람중 autopilot (/venue3d, 2026-11-06): one session = start pose → wander ~1 min → back to the start pose → off
AUTO = {
    "roam_s": 90.0,                  # wandering time before heading home (lobby trips need time, user)
    "hop_mm": (3000.0, 99999.0),     # next point at least this far away (anywhere on the floor, the lobby too)
    "pause_s": (1.5, 4.5),           # standing there a while (looking)
    "meander_deg": 25.0,             # slow side-to-side drift of the heading while walking (not straight lines)
    "plan_s": 0.25,                  # how often the heading is re-planned
    "look_mm": 1800.0,               # look-ahead
    "grid_mm": 150.0,                # where-a-device-may-stand grid
    "gallery": (700.0, 650.0, 10380.0, 19500.0),   # the gallery's inner faces (as the /venue3d cable box): no lobby
}
# Mock-up people (verification only): how they walk. Size and speed come from the venue layer ("people").
PEOPLE_MOTION = {
    "clearance_mm": 150.0,       # kept free around a person, besides its radius
    "turn_deg_s": 120.0,         # turning speed (was 40: a person stuck turning in place looked like looking around, user)
    "accel_mm_s2": 300.0,        # gentle start / stop
    "wait_s": (1.0, 3.0),        # standing still at a reached point
    "publish_hz": 10.0,          # people updates sent to pages at most this often
    "watch_ahead_mm": 4500.0,    # a person this far ahead along a driving device's path steps aside …
    "watch_side_mm": 500.0,      # … if closer to the path than device radius + person radius + this
    "evade_turn_walk_deg": 100.0,
    "stuck_s": 2.5,              # not getting anywhere this long: slip to the nearest clear spot  # while stepping aside a person may start walking while still turning this much
}
# Simple person silhouette (mock-up): legs, torso, arms, head as upright cylinders + a sphere, in the person's own frame
# (fwd along the heading, right to its right), sizes scaled by height / 1700 and radius / 250. Everything stays within
# the person's radius and height. The ToF mock traces these parts; /venue3d draws the same parts.
PERSON_PARTS = [
    {"part": "leg", "type": "cyl", "right": -90, "fwd": 0, "r": 70, "z0": 0, "z1": 820},
    {"part": "leg", "type": "cyl", "right": 90, "fwd": 0, "r": 70, "z0": 0, "z1": 820},
    {"part": "torso", "type": "cyl", "right": 0, "fwd": 0, "r": 185, "z0": 800, "z1": 1430},
    {"part": "arm", "type": "cyl", "right": -195, "fwd": 0, "r": 50, "z0": 860, "z1": 1400},
    {"part": "arm", "type": "cyl", "right": 195, "fwd": 0, "r": 50, "z0": 860, "z1": 1400},
    {"part": "head", "type": "sph", "right": 0, "fwd": 0, "r": 120, "z": 1580},
]


def person_parts(x: float, y: float, heading: float, radius: float, height: float) -> list[dict]:
    """World-placed silhouette parts of one person (plan mm, z up from the floor)."""
    h = math.radians(heading)
    fx, fy, rx, ry = math.sin(h), math.cos(h), math.cos(h), -math.sin(h)
    kz, kr = height / 1700.0, radius / 250.0
    out = []
    for p in PERSON_PARTS:
        cx, cy = x + (p["fwd"] * fx + p["right"] * rx) * kr, y + (p["fwd"] * fy + p["right"] * ry) * kr
        if p["type"] == "cyl":
            out.append({"part": p["part"], "type": "cyl", "x": cx, "y": cy, "r": p["r"] * kr, "z0": p["z0"] * kz, "z1": p["z1"] * kz})
        else:
            out.append({"part": p["part"], "type": "sph", "x": cx, "y": cy, "r": p["r"] * kr, "z": p["z"] * kz})
    return out


def _approach(value: float, target: float, accel: float, decel: float, dt: float) -> float:
    """Move value toward target: accelerate when speeding up in the same direction, otherwise decelerate."""
    speeding_up = abs(target) > abs(value) and (value == 0 or (value > 0) == (target > 0))
    step = (accel if speeding_up else decel) * dt
    return target if abs(target - value) <= step else value + math.copysign(step, target - value)


class SimulatorStore:
    def __init__(self, path: Path, venue=None, mover: bool = True, people: bool = True) -> None:
        # mover=False: no background motion thread (tests advance the motion with step())
        self.path = path
        self.venue = venue  # venue.Venue or None (test room)
        if venue is not None:
            from venue import DEVICE_RADIUS_MM
            self._device_r = DEVICE_RADIUS_MM
        self.venue_id = venue.id if venue is not None else "testroom"
        self.start = copy.deepcopy(venue.start if venue is not None else TESTROOM_START)
        self.condition = threading.Condition()
        self.write_lock = threading.Lock()
        self.devices = copy.deepcopy(self.start)
        self.hyeol_attached = True  # simulation switch (see module doc); True keeps the earlier behaviour
        self.layout_revision = 0     # exhibition layout (venue.py): changes with every accepted {"op": "layout"}
        self.motion = {d: {"v": 0.0, "s": 0.0, "w": 0.0, "move": 0, "strafe": 0, "turn": 0, "input_at": 0.0, "blocked": None, "sliding": None}
                       for d in DEVICES}
        # 관람중 (2026-11-06, /venue3d): a visitor inside the device walks it around — the device drives itself with the
        # same MOTION (speeds, inertia, collisions) toward random free points, pausing now and then. Not saved.
        self.auto = {d: False for d in DEVICES}
        self.rider = {d: False for d in DEVICES}   # a visitor inside (stays when a page takes the device over by hand)
        self.visits = True   # people come in, look and leave (VISIT); False: they wander their own areas (checks)
        # 모드 (user 2026-11-06): None | "collect" (혈 / 맥 start viewing sessions at random times, again and again) |
        # "learn" / "replay" (혈 / 맥 drive back to their parking spots and stay). 심's light pattern follows the mode on
        # the pages. Not saved.
        self.mode = None
        self._next_session = {d: 0.0 for d in DEVICES}
        self._manual_at = {d: -1e9 for d in DEVICES}
        self._auto = {d: {"tx": None, "ty": None, "wait": 0.0, "pause_until": 0.0, "blocked_s": 0.0} for d in DEVICES}
        self._grids = {}   # lookup grids (where a device may stand, where a person may step) for the current layout
        self._grid_busy = False
        self._people_dirty = False
        self._extras_added = False
        self._now = time.monotonic()
        self._tape_cache = (None, [])
        self._auto_rng = random.Random(11)
        self.source = "default"
        self.revision = self.saved_revision = self.version = 0
        self.origin: str | None = None
        self.seq: int | None = None
        self.final = True
        self.save_error: str | None = None
        self._dirty_at: float | None = None
        self._stop = threading.Event()
        self.people = self._make_people() if people else []
        self._people_rng = random.Random(7)
        self._people_pub_at = 0.0
        self._load()
        threading.Thread(target=self._writer, daemon=True).start()
        if mover:
            threading.Thread(target=self._mover, daemon=True).start()

    # --- mock-up people ------------------------------------------------------------------------------------------
    def _make_people(self) -> list[dict]:
        cfg = self.venue.people if self.venue is not None else None
        if not cfg:
            return []
        lo, hi = cfg.get("speed_mm_s", [300, 300])
        out = []
        for i, p in enumerate(cfg["list"]):
            out.append({"id": p["id"], "x": float(p["start"][0]), "y": float(p["start"][1]), "heading": float(p.get("heading", 0)) % 360.0,
                        "v": 0.0, "area": [float(a) for a in p["area"]], "speed": lo + (hi - lo) * (i % 3) / 2,
                        "r": float(cfg["radius_mm"]), "h": float(cfg["height_mm"]), "tx": None, "ty": None, "wait": 0.0,
                        "evading": False, "calm": 0.0, "cornered": False})
        return out

    def people_for_tof(self) -> list[dict]:
        """Each person as {x, y, r, h, parts} (parts = person_parts(), the same silhouette /venue3d draws)."""
        with self.condition:
            return [{"x": p["x"], "y": p["y"], "r": p["r"], "h": p["h"], "parts": person_parts(p["x"], p["y"], p["heading"], p["r"], p["h"])}
                    for p in self.people]

    def _person_blocked(self, p: dict, x: float, y: float) -> bool:
        """Would the person at (x, y) be too close to anything? Moving away from a device or another person that is
        already too close is allowed (otherwise a person a device drove up to could never step away). A person already
        inside the clearance of a wall (squeezed between a device and a wall, 2026-11-06 user: "사람이 끼어서 안 움직여")
        only has to keep clear of overlapping (no extra gap), so every step is not refused."""
        clear = PEOPLE_MOTION["clearance_mm"]
        if self.venue is not None:
            tight = not self._grid_ok("pc", p["x"], p["y"])
            if not self._grid_ok("pt" if tight else "pc", x, y):
                return True
        for k, d in self.devices.items():
            m = self.motion[k]
            lim = self._device_r + p["r"] + clear + (500 if self.auto.get(k) else 0)   # wider from one walking about (관람 session)
            dn = math.hypot(x - d["x"], y - d["y"])
            if dn < lim and dn < math.hypot(p["x"] - d["x"], p["y"] - d["y"]) - 1e-6:
                return True
        for o in self.people:
            if o is p:
                continue
            lim = o["r"] + p["r"] + clear
            dn = math.hypot(x - o["x"], y - o["y"])
            if dn < lim and dn < math.hypot(p["x"] - o["x"], p["y"] - o["y"]) - 1e-6:
                return True
        return False

    def _device_paths(self) -> list[tuple[float, float, float, float]]:
        """(x, y, ux, uy) for every device that is driving: its position and travel direction (from the held input,
        or from its velocity while it still moves)."""
        out = []
        for d, m in self.motion.items():
            p = self.devices[d]
            h = math.radians(p["heading"])
            fwd, side = (m["move"] or (m["v"] / 500.0)), (m["strafe"] or (m["s"] / 300.0))
            vx, vy = fwd * math.sin(h) + side * math.cos(h), fwd * math.cos(h) - side * math.sin(h)
            n = math.hypot(vx, vy)
            if n > 0.04:
                out.append((p["x"], p["y"], vx / n, vy / n))
        return out

    def _threat(self, p: dict, paths) -> tuple[float, float, float, float, float] | None:
        """The driving device whose path this person stands in (nearest ahead), as (x, y, ux, uy, lateral sign)."""
        best = None
        for x, y, ux, uy in paths:
            rx, ry = p["x"] - x, p["y"] - y
            along, lateral = rx * ux + ry * uy, rx * uy - ry * ux
            near = math.hypot(rx, ry) < self._device_r + p["r"] + PEOPLE_MOTION["clearance_mm"] + 150
            if near or (-200 < along < PEOPLE_MOTION["watch_ahead_mm"] and abs(lateral) < self._device_r + p["r"] + PEOPLE_MOTION["watch_side_mm"]):
                if best is None or along < best[0]:
                    best = (along, (x, y, ux, uy, 1.0 if lateral >= 0 else -1.0))
        return best[1] if best else None

    def _evade_target(self, p: dict, t) -> tuple[float, float] | None:
        """A free point out of the device's path: sideways (the person's own side first), then a little further on."""
        x, y, ux, uy, side = t
        nx, ny = uy, -ux                        # unit normal; lateral > 0 is on this side
        rx, ry = p["x"] - x, p["y"] - y
        along = rx * ux + ry * uy
        out = self._device_r + p["r"] + PEOPLE_MOTION["watch_side_mm"] + 250
        for sgn in (side, -side):
            if sgn != side and along < 6000:   # never across the path of a device that is this close (it would be hit)
                continue
            for extra in (0, 600, 1200, -400):
                tx = x + ux * max(along + extra, 0) + nx * sgn * out
                ty = y + uy * max(along + extra, 0) + ny * sgn * out
                steps = max(2, int(math.hypot(tx - p["x"], ty - p["y"]) / 150))
                if all(not self._person_blocked(p, p["x"] + (tx - p["x"]) * k / steps, p["y"] + (ty - p["y"]) * k / steps)
                       for k in range(1, steps + 1)):
                    return tx, ty
        return None

    def _free_spot(self, p: dict) -> tuple[float, float] | None:
        """The nearest point (rings of 16 directions, 0.4–2.5 m) where the person stands clear of everything."""
        clear = PEOPLE_MOTION["clearance_mm"]
        for rad in range(400, 2600, 300):
            for k in range(16):
                a = 2 * math.pi * k / 16
                x, y = p["x"] + rad * math.sin(a), p["y"] + rad * math.cos(a)
                if self.venue is not None and not self._grid_ok("pc", x, y):
                    continue
                if any(math.hypot(x - d["x"], y - d["y"]) < self._device_r + p["r"] + clear for d in self.devices.values()):
                    continue
                if any(o is not p and math.hypot(x - o["x"], y - o["y"]) < o["r"] + p["r"] + clear for o in self.people):
                    continue
                return x, y
        return None

    # --- visits (user 2026-11-06: "들어와서 감상하고 나가는 것처럼") ------------------------------------------------
    def _doorways(self):
        """(lobby door point, lobby side of the gallery entrance, gallery side of it) — the entrance is the run of empty
        lobby window bays in the current layout; None when there is none (then nobody comes or goes)."""
        if self.venue is None or not self.venue.layout_spec.get("windows"):
            return None
        wn, L = self.venue.layout_spec["windows"], self.venue.layout or {}
        empty = [b for b in range(1, wn["bays"] + 1) if b not in (L.get("windows") or wn.get("default_bays", []))]
        if not empty:
            return None
        y = wn["bay_y0"] + wn["bay_w"] * ((min(empty) + max(empty)) / 2 - 0.5)
        return VISIT["door"], (min(wn["x"]) - 1400.0, y), (max(wn["x"]) + 1400.0, y)

    def _look_spot(self, p: dict, what: str):
        """A free place to stand looking at what (심 / a device / around), as (x, y, face x, face y)."""
        rng = self._people_rng
        if what == "sim":
            L = self.venue.layout if self.venue is not None else None
            c = (L or {}).get("sim") or {"x": 5700.0, "y": 9970.0}
            ox, oy, rad = c["x"], c["y"], rng.uniform(*VISIT["sim_mm"])
        elif what in self.devices and (self.auto.get(what) or self.rider.get(what)):   # out walking / ridden: look at 심 instead (user: people
            return self._look_spot(p, "sim")                      # stood watching it from the entrance)
        elif what in self.devices:
            ox, oy = self.devices[what]["x"], self.devices[what]["y"]
            rad = rng.uniform(*VISIT["device_mm"])
        else:
            g = AUTO["gallery"]
            for _ in range(20):
                x, y = rng.uniform(g[0] + 600, g[2] - 600), rng.uniform(g[1] + 600, g[3] - 600)
                if not self._person_blocked(p, x, y):
                    return x, y, x + rng.uniform(-1, 1), y + rng.uniform(-1, 1)
            return None
        for _ in range(16):
            a = rng.uniform(0, 2 * math.pi)
            x, y = ox + rad * math.sin(a), oy + rad * math.cos(a)
            g = AUTO["gallery"]
            taken = any(o is not p and (math.hypot(x - o["x"], y - o["y"]) < 900 or (o.get("tx") is not None and math.hypot(x - o["tx"], y - o["ty"]) < 900))
                        for o in self.people)   # not where someone stands or is going to stand (user: two people rubbing)
            ways = self._doorways()   # … nor in the way in / out
            taken = taken or bool(ways and min(math.hypot(x - w[0], y - w[1]) for w in ways[1:]) < VISIT["keep_door_mm"])
            if g[0] + 300 < x < g[2] - 300 and g[1] + 300 < y < g[3] - 300 and not taken and not self._person_blocked(p, x, y):
                return x, y, ox, oy
        return None

    def _new_plan(self, p: dict, entering: bool) -> list:
        """One visit: (in through the door,) 2–4 looks — 심 most often, 혈 / 맥, now and then just around — then out."""
        rng, plan, ways = self._people_rng, [], self._doorways()
        if entering and ways:
            plan += [("go", *ways[1]), ("go", *ways[2])]
        for _ in range(rng.randint(*VISIT["looks"])):
            w = VISIT["weights_sim"] if self.mode in ("learn", "replay") else VISIT["weights"]   # 학습 / 재현: round 심 (user)
            plan.append(("look", rng.choices(["sim", "hyeol", "maek", "around"], weights=w)[0]))
        if ways:
            plan += [("go", *ways[2]), ("go", *ways[1]), ("go", *ways[0]), ("leave",)]
        return plan

    def _next_target(self, p: dict) -> bool:
        """Set the person's next target from its plan (planning a visit when there is none). False: nothing now."""
        if not p.get("plan"):
            p["plan"] = self._new_plan(p, entering=False)
        while p["plan"]:
            item = p["plan"].pop(0)
            if item[0] == "go":
                p["tx"], p["ty"], p["face"], p["item"] = item[1], item[2], None, item
                return True
            if item[0] == "look":
                spot = self._look_spot(p, item[1])
                if spot is not None:
                    p["tx"], p["ty"], p["face"], p["item"] = spot[0], spot[1], (spot[2], spot[3]), item
                    return True
            if item[0] == "board":   # walking up to a device to get on
                spot = self._board_spot(p, item[1]) if self.mode == "collect" and not self.auto.get(item[1]) else None
                if spot is not None:
                    p["tx"], p["ty"], p["face"], p["item"] = spot[0], spot[1], None, item
                    return True
                if p.get("board"):
                    self._next_session[p["board"]] = self._now + 3.0
                p["board"] = None
                continue
            if item[0] == "leave":   # out of the door: gone for a while, then a new visitor comes in
                p["away"] = self._people_rng.uniform(*VISIT["away_s"])
                p["x"], p["y"], p["v"], p["tx"], p["ty"], p["item"] = -1e6, -1e6, 0.0, None, None, None
                return False
        return False

    def _detour(self, p: dict, want: float) -> bool:
        """A free direction around what is in the way: the turn from the wanted heading that is free for ~0.6 m."""
        for off in (30, -30, 55, -55, 80, -80, 110, -110, 140, -140):
            h = math.radians(want + off)
            if all(not self._person_blocked(p, p["x"] + k * math.sin(h), p["y"] + k * math.cos(h)) for k in (150, 300, 450, 600)):
                p["detour"], p["detour_t"] = (want + off) % 360.0, 2.0
                return True
        return False

    def _step_people(self, dt: float) -> bool:
        rng, moved = self._people_rng, False
        if self.visits and self.venue is not None and self.people and len(self.people) < VISIT["count"] and not self._extras_added:
            self._extras_added = True   # more visitors than the venue's mock-up list: they start outside, coming in over time
            base = self.people[0]
            for k in range(len(self.people), VISIT["count"]):
                q = dict(base, id=f"person-{k + 1}", x=-1e6, y=-1e6, v=0.0, tx=None, ty=None, wait=0.0, plan=None, item=None, face=None,
                         board=None, riding=None, held=False, detour_t=0.0, tries=0, last=None, faced=False,
                         speed=base["speed"] * rng.uniform(0.85, 1.2), evading=False, cornered=False, stuck=0.0)
                q["away"] = rng.uniform(3.0, 40.0)
                self.people.append(q)
        paths = self._device_paths()
        # two people overlapping (they should never): push them apart a little each step, where there is room
        act = [q for q in self.people if q.get("away") is None]
        for i, a in enumerate(act):
            for b in act[i + 1:]:
                dx, dy = b["x"] - a["x"], b["y"] - a["y"]
                dd, need = math.hypot(dx, dy), a["r"] + b["r"] + 40
                if dd < need:
                    ux, uy = (dx / dd, dy / dd) if dd > 1 else (1.0, 0.0)
                    push = min(need - dd, 600 * dt) / 2
                    for q, sg in ((a, -1), (b, 1)):
                        if q.get("held"):
                            continue
                        nx, ny = q["x"] + sg * ux * push, q["y"] + sg * uy * push
                        if self.venue is None or self._grid_ok("pt", nx, ny):
                            q["x"], q["y"] = nx, ny
                            moved = True
        for p in self.people:
            if p.get("held") or p.get("riding"):   # taken over by a page (관객 조종) / riding in a device
                continue
            if p.get("away") is not None:   # outside: come back in through the lobby door when the time is up
                p["away"] -= dt
                ways = self._doorways()
                if p["away"] <= 0 and ways and not any(math.hypot(o["x"] - ways[0][0], o["y"] - ways[0][1]) < 900 for o in self.people if o is not p):
                    p["away"] = None
                    p["x"], p["y"] = ways[0]
                    p["heading"] = math.degrees(math.atan2(ways[1][0] - ways[0][0], ways[1][1] - ways[0][1])) % 360.0
                    p["plan"], p["wait"], p["tx"], p["ty"], p["last"] = self._new_plan(p, entering=True), 0.0, None, None, None
                    moved = True
                continue
            # still stuck after STUCK_S (nowhere to step, e.g. pinned by a device against a wall): slip to the nearest
            # clear spot so neither the person nor the device stays stuck
            if p.get("stuck", 0.0) > PEOPLE_MOTION["stuck_s"]:
                spot = self._free_spot(p)
                if spot is not None:
                    p["x"], p["y"] = spot
                    p["tx"] = p["ty"] = None
                    p["v"], p["wait"], p["evading"], p["cornered"] = 0.0, 0.0, False, False
                    p["teleported"] = True   # a deliberate jump (checks skip it)
                    moved = True
                p["stuck"] = 0.0
            zone_rev = self._grids.get("rev")
            if p.get("checked_zone") != zone_rev and self.venue is not None:   # standing in a no-go area (a start point there, a
                p["checked_zone"] = zone_rev                                    # pocket a new layout made): out, once per layout
                if not self._grid_ok("pt", p["x"], p["y"]):
                    spot = self._free_spot(p)
                    if spot is not None:
                        p["x"], p["y"], p["tx"], p["ty"] = spot[0], spot[1], None, None
                        p["teleported"] = True
                        moved = True
            # stuck = not moved since the last step while dodging / cornered / refused everywhere (waiting idle is not)
            here = (p["x"], p["y"])
            if here != p.get("last"):
                p["stuck"], p["nowhere"] = 0.0, False
            elif p["cornered"] or p["evading"] or p.get("nowhere"):
                p["stuck"] = p.get("stuck", 0.0) + dt
            p["last"] = here
            # a device right next to a standing / looking visitor (e.g. 맥 starting a session from where they stand): step
            # away from it, else both wait for each other for ever (user: "맥 옆에 사람이 갇혀 있어")
            if not p["evading"]:
                for k, d in self.devices.items():
                    dd = math.hypot(p["x"] - d["x"], p["y"] - d["y"])
                    standing = abs(self.motion[k]["v"]) + abs(self.motion[k]["s"]) < 50   # a driving device: the path dodge below
                    if dd < self._device_r + p["r"] + 450 and standing:
                        ux, uy = ((p["x"] - d["x"]) / dd, (p["y"] - d["y"]) / dd) if dd > 1 else (1.0, 0.0)
                        tgt = None
                        for ang in (0, 35, -35, 70, -70, 110, -110):
                            a = math.radians(ang)
                            vx, vy = ux * math.cos(a) - uy * math.sin(a), ux * math.sin(a) + uy * math.cos(a)
                            tx, ty = p["x"] + vx * 1300, p["y"] + vy * 1300
                            if not self._person_blocked(p, tx, ty):
                                tgt = (tx, ty)
                                break
                        if tgt is None:
                            tgt = self._free_spot(p)
                        if tgt is not None:
                            if p.get("item") and p.get("plan") is not None:
                                p["plan"].insert(0, p["item"])
                            p["tx"], p["ty"], p["item"], p["face"], p["wait"] = tgt[0], tgt[1], None, None, 0.0
                            p["evading"], p["calm"] = True, 1.0
                        break
            t = self._threat(p, paths) if paths else None
            if t is not None:
                target = self._evade_target(p, t)
                p["cornered"] = target is None
                if target is not None and (not p["evading"] or p["tx"] is None or math.hypot(target[0] - p["tx"], target[1] - p["ty"]) > 400):
                    if p.get("item") and p.get("plan") is not None and not p["evading"]:
                        p["plan"].insert(0, p["item"])
                    p["tx"], p["ty"], p["item"], p["face"] = target[0], target[1], None, None
                p["evading"], p["wait"], p["calm"] = True, 0.0, 1.0
            elif p["evading"]:
                p["calm"] -= dt
                if p["calm"] <= 0:  # the path is clear again: back to wandering inside its own area
                    p["evading"] = p["cornered"] = False
                    p["tx"] = p["ty"] = None
            if p["wait"] > 0:
                p["wait"] -= dt
                if p.get("faced") and any(q.get("held") and q is not p and math.hypot(q["x"] - p["x"], q["y"] - p["y"]) < 4000 and
                                          abs((math.degrees(math.atan2(q["x"] - p["x"], q["y"] - p["y"])) - p["heading"] + 180.0) % 360.0 - 180.0) < 20
                                          for q in self.people):
                    p["faced"] = False   # a page's visitor stepped into the gaze: turn once more (looks past them)
                if p.get("face") and not p.get("faced"):   # standing to look: turn to the work once, then stay still
                    want = math.degrees(math.atan2(p["face"][0] - p["x"], p["face"][1] - p["y"])) % 360.0
                    for q in self.people:   # a page's visitor in the line of gaze, close: look past them (user 2026-11-07: not staring into the 관람 시뮬 view)
                        if q.get("held") and q is not p and math.hypot(q["x"] - p["x"], q["y"] - p["y"]) < 4000:
                            off = (math.degrees(math.atan2(q["x"] - p["x"], q["y"] - p["y"])) - want + 180.0) % 360.0 - 180.0
                            if abs(off) < 30:
                                want = (want - math.copysign(35.0, off)) % 360.0
                    diff = (want - p["heading"] + 180.0) % 360.0 - 180.0
                    turn = max(-90.0 * dt, min(90.0 * dt, diff))
                    p["heading"] = (p["heading"] + turn) % 360.0
                    moved = True
                    if abs(diff) < 2:
                        p["faced"] = True
                if p["wait"] > 0:
                    continue
            if p["tx"] is None:
                if self.venue is not None and self.visits:   # the visit plan (venue): next place to go / look from
                    if not self._next_target(p):
                        if p.get("away") is None:
                            p["wait"], p["nowhere"] = 1.0, True
                        moved = True
                        continue
                else:
                    x0, y0, x1, y1 = p["area"]
                    for _ in range(20):  # a free point inside its own area
                        tx, ty = rng.uniform(x0, x1), rng.uniform(y0, y1)
                        if not self._person_blocked(p, tx, ty):
                            p["tx"], p["ty"] = tx, ty
                            break
                    else:
                        p["wait"], p["nowhere"] = 1.0, True
                        continue
            dx, dy = p["tx"] - p["x"], p["ty"] - p["y"]
            dist = math.hypot(dx, dy)
            want = math.degrees(math.atan2(dx, dy)) % 360.0
            if p.get("detour_t", 0.0) > 0:   # walking around something in the way (see below)
                p["detour_t"] -= dt
                want = p["detour"]
            diff = (want - p["heading"] + 180.0) % 360.0 - 180.0
            turn = max(-PEOPLE_MOTION["turn_deg_s"] * dt, min(PEOPLE_MOTION["turn_deg_s"] * dt, diff))
            if turn:
                p["heading"] = (p["heading"] + turn) % 360.0
                moved = True
            # walk only when roughly facing the target (stepping aside: may start earlier); slow down near it
            facing = PEOPLE_MOTION["evade_turn_walk_deg"] if p["evading"] else 60
            target_v = 0.0 if abs(diff) > facing or dist < 200 else min(p["speed"], dist)
            v_was = p["v"]
            p["v"] = _approach(p["v"], target_v, PEOPLE_MOTION["accel_mm_s2"], PEOPLE_MOTION["accel_mm_s2"], dt)
            if v_was and not p["v"]:
                moved = True   # came to a stop: the pages must hear it (standing pose)
            if p.get("board") and (p.get("item") or ("",))[0] == "board" and dist < 400:   # at the device: on board
                self._board(p, p["board"])
                moved = True
                continue
            if dist < 200 and p["v"] == 0 or (dist < 400 and (p.get("item") or ("",))[0] == "go"):
                looked = (p.get("item") or ("",))[0] == "look"
                p["tries"], p["faced"] = 0, False
                if looked and p["item"][1] in self.devices:   # a device may have moved since: look at where it is now
                    d = p["item"][1]
                    p["face"] = (self.devices[d]["x"], self.devices[d]["y"])
                    if self.rider.get(d):   # someone (a page's visitor) rides it: not into their eyes (user 2026-11-07) — at 심
                        L = (self.venue.layout or {}) if self.venue is not None else {}
                        c = L.get("sim") or {"x": 5700.0, "y": 9970.0}
                        p["face"] = (c["x"], c["y"])
                p["tx"] = p["ty"] = None
                p["item"] = None
                p["wait"] = 0.0 if p["evading"] else rng.uniform(*VISIT["look_s"]) if looked else 0.0 if p.get("plan") is not None and self.venue is not None else rng.uniform(*PEOPLE_MOTION["wait_s"])
                continue
            if p["v"]:
                h = math.radians(p["heading"])
                nx, ny = p["x"] + p["v"] * dt * math.sin(h), p["y"] + p["v"] * dt * math.cos(h)
                if self._person_blocked(p, nx, ny) and self.venue is not None and p.get("plan") is not None \
                        and p.get("detour_t", 0.0) <= 0 and self._detour(p, want):
                    pass   # something in the way: walk around it (the nearest free direction to the side), kept ~2 s
                elif self._person_blocked(p, nx, ny):
                    p["v"] = 0.0                      # stop here and pick another point (turning slowly toward it)
                    if p.get("item") and p.get("plan") is not None:   # a visit: try the same place again in a moment …
                        p["tries"] = p.get("tries", 0) + 1
                        if p["tries"] <= 5:
                            p["plan"].insert(0, p["item"])
                        else:                                       # … but not for ever
                            p["tries"] = 0
                            if p["item"][0] == "board" and p.get("board"):   # could not get to the device: someone else later
                                self._next_session[p["board"]] = self._now + 3.0
                                p["board"] = None
                        p["item"] = None
                    p["tx"] = p["ty"] = None
                    p["nowhere"] = True
                    p["wait"] = 0.0 if p["evading"] else rng.uniform(*PEOPLE_MOTION["wait_s"]) / 2
                else:
                    p["x"], p["y"] = nx, ny
                moved = True
        return moved

    # --- geometry ------------------------------------------------------------------------------------------
    def bounds(self) -> dict:
        if self.venue is not None:
            return dict(self.venue.bounds)
        r = ROOM["room"]
        m = TESTROOM_MARGIN_MM
        return {"x0": r["x0"] + m, "x1": r["x1"] - m, "y0": r["z0"] + m, "y1": r["z1"] - m}

    _device_r = 300.0  # venue.DEVICE_RADIUS_MM (set in __init__ when a venue is given)

    def _device_blocked(self, x: float, y: float) -> str | None:
        """Like blocked(), plus the mock-up people (a driven device cannot pass through a person)."""
        hit = self.blocked(x, y)
        if hit is None:
            for p in self.people:
                if math.hypot(x - p["x"], y - p["y"]) < self._device_r + p["r"]:
                    return p["id"]
        return hit

    def blocked(self, x: float, y: float) -> str | None:
        if self.venue is not None:
            hit = self.venue.blocked(x, y)
            if hit is None and self._in_tape(x, y, self._device_r):
                return "tape"
            return hit
        b = self.bounds()
        return None if b["x0"] <= x <= b["x1"] and b["y0"] <= y <= b["y1"] else "outside"

    def up_distance(self, device: str = "hyeol") -> float | None:
        """Straight-up distance from the 혈 distance sensor (2.0 m) to the ceiling; None if the ceiling is unknown."""
        p = self.pose(device)
        if self.venue is not None:
            return self.venue.up_distance(p["x"], p["y"], SIM_DISTANCE_HEIGHT)
        return vertical_distance_mm(p["x"], p["y"])

    def _v_pose(self, value: object, partial: bool, current: dict | None = None, slack: float = 0.0) -> dict:
        if not isinstance(value, dict) or (partial and not value) or set(value) - {"x", "y", "heading"}:
            raise ValueError("pose: x, y and/or heading")
        b, out = self.bounds(), {}
        for key, lo, hi in (("x", b["x0"], b["x1"]), ("y", b["y0"], b["y1"])):
            if key in value:
                v = value[key]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
                    raise ValueError(f"{key} must be a number in {lo:.0f}–{hi:.0f} mm")
                out[key] = round(float(v), 1)
            elif not partial:
                raise ValueError(f"pose.{key} missing")
        if "heading" in value:
            v = value["heading"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError("heading must be a number (degrees)")
            out["heading"] = round(float(v) % 360.0, 1)
        elif not partial:
            raise ValueError("pose.heading missing")
        pos = dict(current or {}, **out)
        if slack and self.venue is not None:
            from venue import DEVICE_RADIUS_MM
            hit = self.venue.blocked(pos["x"], pos["y"], radius=DEVICE_RADIUS_MM - slack)
        else:
            hit = self.blocked(pos["x"], pos["y"])
        if hit:
            raise ValueError(f"pose collides with {hit}")
        return out

    # --- load / save -------------------------------------------------------------------------------------------
    def _aside(self, path: Path, tag: str) -> None:
        try:
            shutil.copy2(path, path.with_name(f"{path.name}.{tag}-{time.strftime('%Y%m%d-%H%M%S')}"))
        except OSError:
            pass

    def _read(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            saved_venue = data.get("venue", "testroom")  # version 1 files predate venues: test room
            if saved_venue != self.venue_id:
                print(f"Simulator file {path.name} is for venue {saved_venue}, not {self.venue_id}: not used", flush=True)
                if path == self.path:
                    self._aside(path, f"venue-{saved_venue}")
                return None
            layout = None
            if self.venue is not None and data.get("layout") is not None:
                try:  # applied before the poses are checked: the saved poses stand in the saved layout
                    layout = self.venue.check_layout(data["layout"])
                except ValueError as exc:
                    print(f"Simulator: saved layout not usable ({exc}), default layout used", flush=True)
                    layout = self.venue.default_layout()
                self.venue.set_layout(layout)
            devices = data.get("devices")
            if not isinstance(devices, dict) or set(devices) != set(DEVICES):
                raise ValueError(f"devices must contain {list(DEVICES)}")
            poses = {}
            for d in DEVICES:
                try:
                    # 1 mm slack: a device saved touching a surface is rounded to 0.1 mm in the file
                    poses[d] = self._v_pose(devices[d], partial=False, slack=1.0)
                except ValueError as exc:  # e.g. the geometry changed under a saved pose
                    print(f"Simulator: saved {d} pose not usable ({exc}), start pose used", flush=True)
                    poses[d] = dict(self.start[d])
            attached = data.get("hyeol_attached", True)
            if not isinstance(attached, bool):
                raise ValueError("hyeol_attached must be true or false")
            mode = data.get("mode") if data.get("mode") in ("collect", "learn", "replay") else None
            return {"devices": poses, "revision": max(0, int(data.get("revision", 0))), "hyeol_attached": attached, "layout": layout, "mode": mode}
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            print(f"Simulator file ignored ({path.name}): {exc}", flush=True)
            if path == self.path:
                self._aside(path, "corrupt")
            return None

    def _load(self) -> None:
        for path, source in ((self.path, "file"), (self.path.with_name(self.path.name + ".bak"), "backup")):
            data = self._read(path)
            if data:
                self.devices, self.revision, self.source = data["devices"], data["revision"], source
                self.hyeol_attached = data["hyeol_attached"]
                self.mode = data["mode"]   # kept over a restart (user 2026-11-07: 수집 went off after a server restart)
                self.saved_revision = self.revision
                print(f"Simulator ({self.venue_id}) loaded from {path.name} (revision {self.revision})", flush=True)
                return
        if self.venue is not None:
            self.venue.set_layout(self.venue.default_layout())
        print(f"Simulator ({self.venue_id}): no saved file, using start poses", flush=True)

    def _write(self) -> None:
        with self.write_lock:
            with self.condition:
                revision = self.revision
                data = {"version": 2, "venue": self.venue_id, "revision": revision,
                        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "hyeol_attached": self.hyeol_attached,
                        "mode": self.mode, "devices": copy.deepcopy(self.devices)}
                if self.venue is not None:
                    data["layout"] = copy.deepcopy(self.venue.layout)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                if self.path.exists():
                    shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
                for attempt in range(5):  # Windows can briefly lock the target
                    try:
                        os.replace(tmp, self.path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.05)
                error = None
            except OSError as exc:
                error = str(exc)
                print(f"Simulator save failed: {exc}", flush=True)
            with self.condition:
                if error is None:
                    self.saved_revision = max(self.saved_revision, revision)
                    self.source = "file"
                self.save_error = error
                self.version += 1
                self.condition.notify_all()

    def _writer(self) -> None:
        while not self._stop.is_set():
            with self.condition:
                self.condition.wait_for(lambda: self._dirty_at is not None or self._stop.is_set(), timeout=1.0)
                due = self._dirty_at
            if due is None:
                continue
            delay = due + DEBOUNCE_SECONDS - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
                continue
            with self.condition:
                self._dirty_at = None
            self._write()

    def stop(self) -> None:
        with self.condition:
            for m in self.motion.values():
                m.update(v=0.0, s=0.0, w=0.0, move=0, strafe=0, turn=0)
            pending = self.revision != self.saved_revision
            self._dirty_at = None
        if pending:
            self._write()
        self._stop.set()
        with self.condition:
            self.condition.notify_all()

    # --- motion ------------------------------------------------------------------------------------------------
    def _active(self) -> bool:
        return bool(self.people) or bool(self.mode) or any(self.auto.values()) or any(m["v"] or m["s"] or m["w"] or m["move"] or m["strafe"] or m["turn"] for m in self.motion.values())

    def _normal(self, x: float, y: float) -> tuple[float, float] | None:
        """Unit normal of the surface touching a device at (x, y), pointing back toward it."""
        for p in self.people:  # touching a person: push back from its centre
            d = math.hypot(x - p["x"], y - p["y"])
            if 1e-6 < d < self._device_r + p["r"] + 5:
                return (x - p["x"]) / d, (y - p["y"]) / d
        if self.venue is not None:
            return self.venue.contact_normal(x, y)
        b = self.bounds()
        gaps = [(x - b["x0"], (1.0, 0.0)), (b["x1"] - x, (-1.0, 0.0)), (y - b["y0"], (0.0, 1.0)), (b["y1"] - y, (0.0, -1.0))]
        gap, n = min(gaps)
        return n if gap < 5.0 else None

    def _advance(self, x: float, y: float, dx: float, dy: float) -> tuple[float, float, str | None]:
        """Move (x, y) by (dx, dy) as far as it is free. Returns the new position and what stopped it (None = all of it)."""
        hit = self._device_blocked(x + dx, y + dy)
        if hit is None:
            return x + dx, y + dy, None
        lo, hi = 0.0, 1.0
        for _ in range(12):
            mid = (lo + hi) / 2
            if self._device_blocked(x + dx * mid, y + dy * mid) is None:
                lo = mid
            else:
                hi = mid
        return x + dx * lo, y + dy * lo, hit

    def _move(self, p: dict, m: dict, dx: float, dy: float) -> None:
        """Apply one step's displacement with sliding (see module doc). Updates p (pose) and m (motion state)."""
        x, y, hit = self._advance(p["x"], p["y"], dx, dy)
        m["sliding"] = None
        if hit is not None:
            frac = math.hypot(x - p["x"], y - p["y"]) / max(math.hypot(dx, dy), 1e-9)
            rx, ry = dx * (1 - frac), dy * (1 - frac)  # what is left of this step after the contact point
            n = self._normal(x, y)
            stop = n is None
            if n is not None:
                into = rx * n[0] + ry * n[1]
                tx, ty = (rx - into * n[0], ry - into * n[1]) if into < 0 else (rx, ry)
                rest = math.hypot(rx, ry)
                if rest > 1e-9 and math.hypot(tx, ty) < rest * math.sin(math.radians(MOTION["slide_min_angle_deg"])):
                    stop = True  # (nearly) head-on
                elif rest > 1e-9:
                    x2, y2, hit2 = self._advance(x, y, tx, ty)
                    slid = math.hypot(x2 - x, y2 - y)
                    if hit2 is not None and slid < 0.05 * math.hypot(tx, ty):
                        stop, hit = True, hit2  # the slide runs into another surface: a corner
                    else:
                        x, y, m["sliding"] = x2, y2, hit
            if stop:
                m["v"] = m["s"] = 0.0
                m["blocked"] = hit
            else:
                m["blocked"] = None
        else:
            m["blocked"] = None
        p["x"], p["y"] = x, y  # not rounded: rounding could nudge a device touching a surface a hair inside it

    def _start_session(self, d: str, home: dict | None = None, rider: bool = True, phase: str = "roam") -> None:
        """A session of the autopilot: roam (then home) or only "return" (drive to home and stop). Home: where it is now
        unless given (the parking spot)."""
        p = self.devices[d]
        if phase == "return" and home is not None and math.hypot(p["x"] - home["x"], p["y"] - home["y"]) < 60 \
                and abs((p["heading"] - home["heading"] + 180) % 360 - 180) < 4:
            return   # already parked
        self.auto[d], self.rider[d] = True, rider
        self._auto[d].update(tx=None, ty=None, wait=0.0, pause_until=0.0, blocked_s=0.0, head=None, stuck_s=0.0, route=None,
                             phase=phase, left=AUTO["roam_s"], meander=self._auto_rng.uniform(0, 6.3), home=dict(home or p))
        if phase == "return":
            self._auto[d].update(tx=self._auto[d]["home"]["x"], ty=self._auto[d]["home"]["y"])
        self.motion[d].update(move=0, turn=0, strafe=0)

    def _schedule(self, now: float) -> None:
        """수집 (user 2026-11-06): when a device is due, a visitor walks up to it and gets on (then the session runs with
        them riding); at the end they get off behind it and go on with their visit. Driven by hand: the rider stays on
        until MODE["hands_off_s"] without input, then gets off."""
        for d in DEVICES:
            rider = next((q for q in self.people if q.get("riding") == d), None)
            if self.auto[d]:
                self._next_session[d] = max(self._next_session[d], now + self._auto_rng.uniform(*MODE["between_s"]))
                continue
            if self.rider[d] and rider is None:   # a page's own visitor (no server person) on board
                continue
            if rider is not None:   # on board but not in a session (taken over by hand): off after a while untouched
                if not rider.get("held") and now - self._manual_at[d] > MODE["hands_off_s"]:
                    self._unboard(d)
                continue
            for q in self.people:   # called but no longer on the way (their walk up was dropped, they left): free the device
                if q.get("board") == d and not (q.get("item") and q["item"][0] == "board") and ("board", d) not in (q.get("plan") or []):
                    q["board"] = None
                    self._next_session[d] = min(self._next_session[d], now + 3.0)
            if now < self._next_session[d] or now - self._manual_at[d] < MODE["hands_off_s"]:
                continue
            if any(q.get("board") == d for q in self.people):
                continue   # someone is on the way
            free = [q for q in self.people if q.get("away") is None and not q.get("held") and not q.get("riding") and not q.get("board") and not q["evading"]]
            if not free:
                self._next_session[d] = now + 5.0
                continue
            p = self.devices[d]
            q = min(free, key=lambda o: math.hypot(o["x"] - p["x"], o["y"] - p["y"]))
            # the walk up goes first in their plan (what they were doing comes after); _next_target sets the spot
            rest = ([q["item"]] if q.get("item") else []) + list(q.get("plan") or [])
            q["plan"] = [("board", d)] + rest
            q["board"], q["item"], q["tx"], q["ty"], q["wait"], q["face"] = d, None, None, None, 0.0, None
            self._next_session[d] = now + 1e9   # until this session ends

    def _board_spot(self, p: dict, d: str):
        """Just behind the device, outside its parking box (people keep out of it)."""
        q = self.devices[d]
        def clear_walk(x, y):   # a straight walk there from where they are: no wall on the way, not through the device
            L = math.hypot(x - p["x"], y - p["y"]) or 1.0
            t = max(0.0, min(1.0, ((q["x"] - p["x"]) * (x - p["x"]) + (q["y"] - p["y"]) * (y - p["y"])) / (L * L)))
            if math.hypot(p["x"] + (x - p["x"]) * t - q["x"], p["y"] + (y - p["y"]) * t - q["y"]) < self._device_r + p["r"]:
                return False
            return self.venue is None or all(self._grid_ok("pt", p["x"] + (x - p["x"]) * k / L, p["y"] + (y - p["y"]) * k / L) for k in range(0, int(L), 150))
        best = None
        for rank, (dist, off) in enumerate((dist, off) for dist in (1500, 1300, 1800) for off in (0, 35, -35, 70, -70, 110, -110, 145, -145, 180)):
            # behind first, then round it (시안 2: both stand with their backs to a wall, user 2026-11-07); a spot walked to
            # in a straight line wins over one behind (people stuck on the way there)
            h = math.radians(q["heading"] + 180 + off)
            x, y = q["x"] + math.sin(h) * dist, q["y"] + math.cos(h) * dist
            if self._person_blocked(p, x, y) or (self.venue is not None and not self._grid_ok("pc", x, y)):
                continue
            key = (0 if clear_walk(x, y) else 1, rank)
            if best is None or key < best[0]:
                best = (key, (x, y))
            if key[0] == 0:
                break
        return best[1] if best else None

    def _board(self, p: dict, d: str) -> None:
        p["riding"], p["board"], p["item"] = d, None, None
        p["x"], p["y"], p["v"], p["tx"], p["ty"] = -1e6, -1e6, 0.0, None, None   # inside the device (drawn as its rider)
        self._start_session(d, home=self.start[d], rider=True)
        self.version += 1

    def _unboard(self, d: str) -> None:
        """The rider gets off behind the device and goes on with the visit (a free spot near there)."""
        self.rider[d] = False
        q = self.devices[d]
        for p in self.people:
            if p.get("riding") != d:
                continue
            p["riding"] = None
            # behind the device, else round it: a free spot with nothing between it and the device (not over a wall
            # behind the parking spot, user 2026-11-06: "가벽 뒤로 내려지면서 갇혔어")
            def seen(x, y):   # nothing between the spot and the device (sampled up to its parking box, which people keep out of)
                d = math.hypot(x - q["x"], y - q["y"])
                return all(self._grid_ok("pt", x + (q["x"] - x) * k / d, y + (q["y"] - y) * k / d) for k in range(0, int(d - 1150), 100))
            spot = None
            for strict in (True, False):
                for off in (0, 30, -30, 60, -60, 90, -90, 130, -130, 180):
                    for dist in (1300, 1700, 2100):
                        h = math.radians(q["heading"] + 180 + off)
                        x, y = q["x"] + math.sin(h) * dist, q["y"] + math.cos(h) * dist
                        if self._grid_ok("pc", x, y) and (not strict or seen(x, y)):   # pc: pockets behind walls are not in it
                            spot = (x, y, (q["heading"] + 180 + off) % 360)
                            break
                    if spot:
                        break
                if spot:
                    break
            if spot is None:
                h = math.radians(q["heading"] + 180)
                spot = (q["x"] + math.sin(h) * 1300, q["y"] + math.cos(h) * 1300, (q["heading"] + 180) % 360)
            p["x"], p["y"], p["heading"] = spot
            if not self._grid_ok("pc", p["x"], p["y"]):
                spot = self._free_spot(p)
                if spot is not None:
                    p["x"], p["y"] = spot
            p["plan"], p["tx"], p["ty"], p["wait"], p["face"], p["last"] = None, None, None, 0.5, None, None
            plan = self._new_plan(p, entering=False)
            p["plan"] = plan
        self._next_session[d] = self._now + self._auto_rng.uniform(*MODE["between_s"])   # the step's clock (tests run fast)
        self.version += 1

    def _autopilot(self, d: str, m: dict, p: dict, dt: float, now: float) -> None:
        """관람중 (user 2026-11-06): a session — from where the device stands (the apply op), it wanders the gallery
        like a person walking it for AUTO["roam_s"], then walks back to where it started, turns to that heading and
        switches itself off. Inputs only; the motion below (MOTION speeds, inertia, collisions) does the moving. A page
        driving it by hand takes over for a few seconds."""
        a = self._auto[d]
        if now < a["pause_until"]:
            return
        m["input_at"] = now
        a["left"] -= dt
        home = a.get("home") or self.start[d]
        if a["phase"] == "roam" and a["left"] <= 0:
            a["phase"], a["tx"], a["ty"], a["wait"], a["head"], a["route"] = "return", home["x"], home["y"], 0.0, None, None
        if a["phase"] == "return":   # follow a route around everything (grid A*), re-planned every 2 s
            a["route_s"] = a.get("route_s", 0.0) - dt
            if a.get("route") is None or a["route_s"] <= 0:
                a["route"], a["route_s"] = self._route(p["x"], p["y"], home["x"], home["y"]), 2.0
            rt = a["route"] or []
            while len(rt) > 1 and math.hypot(rt[0][0] - p["x"], rt[0][1] - p["y"]) < 700:
                rt.pop(0)
            a["tx"], a["ty"] = rt[0] if rt else (home["x"], home["y"])
            if math.hypot(home["x"] - p["x"], home["y"] - p["y"]) < 1200:   # the last stretch: straight in, slowly
                a["tx"], a["ty"] = home["x"], home["y"]
                dx, dy = home["x"] - p["x"], home["y"] - p["y"]
                dist = math.hypot(dx, dy)
                if dist < 120:   # home: settle exactly on the spot, then turn to the heading
                    p["x"], p["y"] = home["x"], home["y"]
                    a["phase"] = "align"
                    m["move"] = m["turn"] = 0
                    return
                diff = (math.degrees(math.atan2(dx, dy)) - p["heading"] + 180.0) % 360.0 - 180.0
                if abs(diff) > 100:   # it is behind: back in rather than turning round on the spot
                    diff = (diff + 180.0) % 360.0 - 180.0 if abs(diff) < 180 else 0.0
                    m["turn"] = max(-1.0, min(1.0, ((diff + 180) % 360 - 180) / 25.0))
                    m["move"] = -0.5 if abs((diff + 180) % 360 - 180) < 30 else 0
                else:
                    m["turn"] = max(-1.0, min(1.0, diff / 25.0))
                    m["move"] = (0.6 if abs(diff) < 25 else 0) * max(0.35, min(1.0, dist / 900.0))
                if any(math.hypot(q["x"] - p["x"], q["y"] - p["y"]) < self._device_r + q["r"] + 150 for q in self.people):
                    m["move"] = 0   # someone right there: wait
                m["strafe"] = 0
                return
        if a["wait"] > 0:
            a["wait"] -= dt
            m["move"] = m["turn"] = m["strafe"] = 0
            return
        if a["phase"] == "align":   # home: turn to the start heading, then the session ends
            diff = (home["heading"] - p["heading"] + 180.0) % 360.0 - 180.0
            m["move"], m["strafe"] = 0, 0
            m["turn"] = max(-1.0, min(1.0, diff / 20.0))
            if abs(diff) < 3:   # session over: back home, the visitor steps out
                m["turn"] = 0
                self.auto[d] = self.rider[d] = False
                if any(q.get("riding") == d for q in self.people):
                    self._unboard(d)
                self.version += 1
            return
        if a["tx"] is None:   # roam: a free point anywhere on the floor (the lobby too), not too near; reached by a route
            rng, b = self._auto_rng, self.bounds()
            for _ in range(60):
                x, y = rng.uniform(b["x0"], b["x1"]), rng.uniform(b["y0"], b["y1"])
                if math.hypot(x - p["x"], y - p["y"]) > AUTO["hop_mm"][0] and self._grid_free(x, y):
                    a["route"] = self._route(p["x"], p["y"], x, y)
                    a["tx"], a["ty"], a["head"] = x, y, None
                    break
            else:
                a["wait"] = 0.5
                return
        if a["phase"] == "roam" and a.get("route"):   # follow the route's points (round walls, through the entrance)
            rt = a["route"]
            while len(rt) > 1 and math.hypot(rt[0][0] - p["x"], rt[0][1] - p["y"]) < 700:
                rt.pop(0)
            a["goal"] = (a["tx"], a["ty"])
        dx, dy = (a["route"][0][0] - p["x"], a["route"][0][1] - p["y"]) if a["phase"] == "roam" and a.get("route") and len(a["route"]) > 1 else (a["tx"] - p["x"], a["ty"] - p["y"])
        dist = math.hypot(dx, dy)
        if a["phase"] == "roam" and math.hypot(a["tx"] - p["x"], a["ty"] - p["y"]) < 450:
            a["tx"] = None
            a["wait"] = self._auto_rng.uniform(*AUTO["pause_s"])
            m["move"] = m["turn"] = 0
            return
        # steer: every AUTO["plan_s"], the free heading closest to the target (with a slow meander, so it does not just
        # go straight) from a fan of headings, judged by the free run ahead — walls / pieces from the grid, people and
        # the other device directly; ease off near something, stop short of it
        a["plan_s"] = a.get("plan_s", 0.0) - dt
        a["meander"] = a.get("meander", 0.0) + dt
        if a["plan_s"] <= 0 or a.get("head") is None:
            a["plan_s"] = AUTO["plan_s"]
            want = math.degrees(math.atan2(dx, dy))
            if a["phase"] == "roam" and dist > 900:
                want += AUTO["meander_deg"] * math.sin(a["meander"] * 0.8)
            reach = min(AUTO["look_mm"], dist)
            best, best_free = None, -1.0
            for off in (0, 15, -15, 30, -30, 45, -45, 60, -60, 90, -90, 120, -120, 150, -150, 180):
                free = self._free_ahead(d, p["x"], p["y"], want + off, reach)
                if free >= reach - 1:
                    best, best_free = want + off, free
                    break
                if free > best_free:
                    best, best_free = want + off, free
            a["head"], a["free"] = best, best_free
        diff = (a["head"] - p["heading"] + 180.0) % 360.0 - 180.0
        free = a["free"]
        m["turn"] = max(-1.0, min(1.0, diff / 25.0))
        slow = 0.0 if free < 350 else min(1.0, free / 1200.0)
        if a["phase"] == "return":   # coming home: slow down at the end; the home spot itself counts as free
            slow = min(1.0, max(0.3, dist / 1500.0)) if free >= min(350, dist - 1) else 0.0
        m["move"] = (1 if abs(diff) < 30 else (0.4 if abs(diff) < 70 else 0)) * slow
        m["strafe"] = 0
        if free < 350:   # boxed in this way: keep turning toward the freest heading; after a while, somewhere else
            a["stuck_s"] = a.get("stuck_s", 0.0) + dt
            if a["stuck_s"] > 4.0:
                a["stuck_s"], a["wait"], a["head"] = 0.0, 0.5, None
                if a["phase"] == "roam":
                    a["tx"] = None
        else:
            a["stuck_s"] = 0.0

    def _route(self, x0: float, y0: float, x1: float, y1: float) -> list | None:
        """Grid A* (8 neighbours) from (x0, y0) to (x1, y1) over where a device may stand; every 4th cell as a point."""
        import heapq
        self._grid_ok("dev", x0, y0)
        box, G, cells = self._grids["dev"]
        ny_, nx_ = len(cells), len(cells[0])
        cell = lambda x, y: (min(nx_ - 1, max(0, int((x - box[0]) // G))), min(ny_ - 1, max(0, int((y - box[1]) // G))))

        def nearest_free(c):
            if cells[c[1]][c[0]]:
                return c
            for r in range(1, 12):
                for i in range(c[0] - r, c[0] + r + 1):
                    for j in (c[1] - r, c[1] + r):
                        if 0 <= i < nx_ and 0 <= j < ny_ and cells[j][i]:
                            return (i, j)
                for j in range(c[1] - r, c[1] + r + 1):
                    for i in (c[0] - r, c[0] + r):
                        if 0 <= i < nx_ and 0 <= j < ny_ and cells[j][i]:
                            return (i, j)
            return None
        s0, s1 = nearest_free(cell(x0, y0)), nearest_free(cell(x1, y1))
        if s0 is None or s1 is None:
            return None
        h = lambda c: math.hypot(c[0] - s1[0], c[1] - s1[1])
        openq, came, cost = [(h(s0), s0)], {s0: None}, {s0: 0.0}
        while openq:
            _, c = heapq.heappop(openq)
            if c == s1:
                break
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                n = (c[0] + di, c[1] + dj)
                if not (0 <= n[0] < nx_ and 0 <= n[1] < ny_) or not cells[n[1]][n[0]]:
                    continue
                nc = cost[c] + (1.4142 if di and dj else 1.0)
                if nc < cost.get(n, 1e18):
                    cost[n], came[n] = nc, c
                    heapq.heappush(openq, (nc + h(n), n))
        if s1 not in came:
            return None
        path, c = [], s1
        while c is not None:
            path.append((box[0] + (c[0] + 0.5) * G, box[1] + (c[1] + 0.5) * G))
            c = came[c]
        path.reverse()
        return path[::4] + [path[-1]]

    def _tape_zones(self) -> list:
        """The taped no-go areas on the floor (/venue3d draws the same tape): around 심 (a hexagon TAPE["sim_mm"]
        outside its plate), around its PC, along the PC → 심 cable (the shortest right-angle route to the nearest tower
        foot, its bend on the wall side, as /venue3d). Polygons, plan mm; cached per layout."""
        if self.venue is None or not self.venue.layout:
            return []
        if self._tape_cache[0] == self.layout_revision:
            return self._tape_cache[1]
        L, sp, out = self.venue.layout, self.venue.layout_spec, []
        sim = L.get("sim")
        if sim:
            a0, R = math.radians(sim["heading"]), 1060.0 + TAPE["sim_mm"]
            hexa = []
            for k in range(6):
                u, v = R * math.cos(k * math.pi / 3), R * math.sin(k * math.pi / 3)
                hexa.append((sim["x"] + u * math.cos(a0) + v * math.sin(a0), sim["y"] - u * math.sin(a0) + v * math.cos(a0)))
            out.append(hexa)
        pc = L.get("pc")
        if pc and sp.get("pc"):
            out.append(self._rect(pc["x"], pc["y"], math.radians(pc["heading"]), sp["pc"]["size"][0] / 2 + TAPE["pc_mm"], sp["pc"]["size"][1] / 2 + TAPE["pc_mm"]))
            feet = []
            for sd in self.venue.solids:
                if sd["id"].startswith("sim-leg-") and sd["id"].endswith("-1"):
                    pl = sd["poly"]
                    feet.append((float(sum(q[0] for q in pl) / len(pl)), float(sum(q[1] for q in pl) / len(pl))))
            if feet:
                B = TAPE["cable_box"]
                gap = lambda q: min(abs(q[0] - B[0]), abs(B[2] - q[0]), abs(q[1] - B[1]), abs(B[3] - q[1]))
                ft = min(feet, key=lambda f: abs(f[0] - pc["x"]) + abs(f[1] - pc["y"]))
                bend = min(((ft[0], pc["y"]), (pc["x"], ft[1])), key=gap)
                for a, b in (((pc["x"], pc["y"]), bend), (bend, ft)):
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    n = math.hypot(dx, dy)
                    if n < 1:
                        continue
                    h = TAPE["cable_mm"]
                    ux, uy = dx / n, dy / n
                    a2, b2 = (a[0] - ux * h, a[1] - uy * h), (b[0] + ux * h, b[1] + uy * h)
                    out.append([(a2[0] - uy * h, a2[1] + ux * h), (b2[0] - uy * h, b2[1] + ux * h), (b2[0] + uy * h, b2[1] - ux * h), (a2[0] + uy * h, a2[1] - ux * h)])
        self._tape_cache = (self.layout_revision, out)
        return out

    @staticmethod
    def _rect(x, y, ang, hw, hh):
        c, s2 = math.cos(ang), math.sin(ang)
        return [(x + u * c + v * s2, y - u * s2 + v * c) for u, v in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]

    def _in_tape(self, x: float, y: float, r: float) -> bool:
        """Would a circle of radius r at (x, y) cross into a taped area?"""
        for pl in self._tape_zones():
            inside = False
            for k in range(len(pl)):
                (ax, ay), (bx, by) = pl[k], pl[(k + 1) % len(pl)]
                if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
                    inside = not inside
                ex, ey = bx - ax, by - ay
                t = max(0.0, min(1.0, ((x - ax) * ex + (y - ay) * ey) / max(ex * ex + ey * ey, 1e-9)))
                if math.hypot(x - ax - ex * t, y - ay - ey * t) < r:
                    return True
            if inside:
                return True
        return False

    def _grid_free(self, x: float, y: float) -> bool:
        """Can a device stand at (x, y)? From a 150 mm grid of where it may stand for the current layout (venue.blocked
        itself costs ~6 ms, too slow for looking ahead every tick); the gallery only (not the lobby)."""
        if self.venue is None:
            return self.blocked(x, y) is None
        return self._grid_ok("dev", x, y)

    def _grid_ok(self, kind: str, x: float, y: float) -> bool:
        """Grid lookups instead of venue.blocked (~6 ms a call; people and the autopilot test many points a tick).
        dev: a device may stand here (gallery only). pc / pt: a person keeps the clearance / at least does not overlap
        (whole walkable area, overhead counted). Each grid is the exact test at the cell centre with the radius grown by
        the cell's half diagonal, so a lookup never lets anything closer than the exact test would. Rebuilt when the
        layout changes."""
        gs = self._grids
        if gs.get("rev") != self.layout_revision and not self._grid_busy and gs:
            # the layout changed: rebuild all grids in the background (~2 s), the old ones serve meanwhile — rebuilding
            # inline would stall the simulation on every step of a wall being dragged
            self._grid_busy = True
            rev = self.layout_revision
            threading.Thread(target=lambda: self._rebuild_grids(rev), daemon=True).start()
        gr = gs.get(kind)
        if gr is None:   # first use: build them now (the people grids together: pockets pruned)
            if kind == "dev":
                gr = gs[kind] = self._grid_for(kind)
            else:
                pg = {"pc": self._grid_for("pc"), "pt": self._grid_for("pt")}
                self._prune_pockets(pg)
                gs.update(pg)
                gr = gs[kind]
            gs.setdefault("rev", self.layout_revision)
        box, G, cells = gr
        i, j = int((x - box[0]) // G), int((y - box[1]) // G)
        return 0 <= j < len(cells) and 0 <= i < len(cells[0]) and cells[j][i]

    def _grid_for(self, kind: str):
        b = self.venue.bounds
        r0 = self.people[0]["r"] if self.people else 250.0
        box, G, rad, over = {"dev": ((b["x0"], b["y0"], b["x1"], b["y1"]), AUTO["grid_mm"], self._device_r, False),
                             "pc": ((b["x0"], b["y0"], b["x1"], b["y1"]), AUTO["grid_mm"], r0 + PEOPLE_MOTION["clearance_mm"], True),
                             "pt": ((b["x0"], b["y0"], b["x1"], b["y1"]), AUTO["grid_mm"], r0, True)}[kind]
        # devices: + the cell's half diagonal (never closer than the exact test); people: + half a cell (at most ~3 cm closer
        # than exact) — the full margin made the gap between a person and a wall look shut, and they dodged across the path
        cells = self._build_grid(box, G, rad + G * (0.71 if kind == "dev" else 0.5), over, people=kind != "dev")
        if kind == "dev":   # devices stay in the gallery (a session came home through the lobby, user 2026-11-07)
            gx0, gy0, gx1, gy1 = AUTO["gallery"]
            for j, row in enumerate(cells):
                y = box[1] + (j + 0.5) * G
                for i in range(len(row)):
                    x = box[0] + (i + 0.5) * G
                    if not (gx0 - 400 < x < gx1 + 400 and gy0 - 400 < y < gy1 + 400):
                        row[i] = False
        return box, G, cells

    def _rebuild_grids(self, rev: int) -> None:
        try:
            gs = {"rev": rev, "dev": self._grid_for("dev"), "pc": self._grid_for("pc"), "pt": self._grid_for("pt")}
            self._prune_pockets(gs)
            self._grids = gs
        finally:
            self._grid_busy = False

    def _prune_pockets(self, gs: dict) -> None:
        """People keep out of pockets (user 2026-11-06: "가벽 뒤 공간으로 못 들어가게"): on the clearance grid only what
        joins the main floor stays — the part with the entrance (else the largest) — so a space behind a wall reached
        only through a gap narrower than a person with its clearance drops out. The overlap grid keeps only what lies
        within two cells of that."""
        import numpy as np
        box, G, pc = gs["pc"]
        A = np.array(pc, bool)
        ny_, nx_ = A.shape
        comp = np.full(A.shape, -1, int)
        sizes = []
        for j0 in range(ny_):
            for i0 in range(nx_):
                if not A[j0, i0] or comp[j0, i0] >= 0:
                    continue
                k, n, stack = len(sizes), 0, [(i0, j0)]
                comp[j0, i0] = k
                while stack:
                    i, j = stack.pop()
                    n += 1
                    for a, b in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                        if 0 <= a < nx_ and 0 <= b < ny_ and A[b, a] and comp[b, a] < 0:
                            comp[b, a] = k
                            stack.append((a, b))
                sizes.append(n)
        if not sizes:
            return
        keep = {int(np.argmax(sizes))}
        dw = self._doorways() if self.venue is not None else None
        for q in ((dw[1], dw[2]) if dw else ()):
            i, j = int((q[0] - box[0]) // G), int((q[1] - box[1]) // G)
            if 0 <= i < nx_ and 0 <= j < ny_ and comp[j, i] >= 0:
                keep.add(int(comp[j, i]))
        main = np.isin(comp, list(keep))
        near = main.copy()
        for _ in range(2):
            n2 = near.copy()
            n2[1:, :] |= near[:-1, :]; n2[:-1, :] |= near[1:, :]; n2[:, 1:] |= near[:, :-1]; n2[:, :-1] |= near[:, 1:]
            near = n2
        gs["pc"] = (box, G, main.tolist())
        bt, Gt, pt = gs["pt"]
        gs["pt"] = (bt, Gt, (np.array(pt, bool) & near).tolist())

    def _parking_zones(self) -> list:
        """혈 / 맥 parking boxes (their start spots, 1.6 m squares as /venue3d's tape): people keep out (user)."""
        return [self._rect(q["x"], q["y"], math.radians(q["heading"]), 800.0, 800.0) for q in self.start.values()]

    def _build_grid(self, g, G, r, overhead, people=False) -> list:
        """The same test as venue.blocked (a circle of radius r against every solid — below the device height unless
        overhead — and the walkable outline), vectorised over the cell centres."""
        import numpy as np
        xs, ys = np.arange(g[0], g[2], G) + G / 2, np.arange(g[1], g[3], G) + G / 2
        X, Y = np.meshgrid(xs, ys)
        P = np.stack([X.ravel(), Y.ravel()], 1)
        bad = np.zeros(len(P), bool)

        def inside(poly):
            res = np.zeros(len(P), bool)
            for k in range(len(poly)):
                (ax, ay), (bx, by) = poly[k], poly[(k + 1) % len(poly)]
                cond = (ay > P[:, 1]) != (by > P[:, 1])
                xc = ax + (P[:, 1] - ay) * (bx - ax) / (by - ay if by != ay else 1e-9)
                res ^= cond & (P[:, 0] < xc)
            return res

        def near(poly):
            dmin = np.full(len(P), np.inf)
            for k in range(len(poly)):
                a_, b_ = poly[k], poly[(k + 1) % len(poly)]
                ab = b_ - a_
                t = np.clip(((P - a_) @ ab) / max(float(ab @ ab), 1e-9), 0, 1)
                dmin = np.minimum(dmin, np.hypot(*(P - (a_ + t[:, None] * ab)).T))
            return dmin < r
        floor = self.venue.floor_z
        for sd in self.venue.solids:
            if sd["z0"] >= floor + (2200 if overhead else 2350):   # passes under it (a person: beams / heads above 2.2 m)
                continue
            pl = np.asarray(sd["poly"], float)
            if pl[:, 0].max() + r < g[0] or pl[:, 0].min() - r > g[2] or pl[:, 1].max() + r < g[1] or pl[:, 1].min() - r > g[3]:
                continue
            bad |= inside(pl) | near(pl)
        for tz in self._tape_zones() + (self._parking_zones() if people else []):   # taped no-go areas (+ parking for people)
            pl = np.asarray(tz, float)
            bad |= inside(pl) | near(pl)
        w = np.asarray(self.venue.walkable, float)
        bad |= ~inside(w) | near(w)
        return (~bad).reshape(len(ys), len(xs)).tolist()

    def _free_ahead(self, d: str, x: float, y: float, heading: float, reach: float) -> float:
        """How far (mm) device d can go from (x, y) along heading before a wall / piece, a person (with a margin so it
        passes them politely) or the other device."""
        h = math.radians(heading)
        ux, uy = math.sin(h), math.cos(h)
        others = [q for k, q in self.devices.items() if k != d]
        step = 150.0
        t = step
        while t <= reach:
            px, py = x + ux * t, y + uy * t
            if not self._grid_free(px, py):
                return t - step
            if any(math.hypot(px - q["x"], py - q["y"]) < self._device_r + q["r"] + 350 for q in self.people):
                return t - step
            if any(math.hypot(px - q["x"], py - q["y"]) < 2 * self._device_r + 250 for q in others):
                return t - step
            t += step
        return reach

    def step(self, dt: float, now: float | None = None) -> bool:
        """Advance every device by dt seconds (the mover thread calls this; tests call it directly).
        Returns True when a pose changed."""
        now = time.monotonic() if now is None else now
        self._now = now
        changed, resting = False, True
        with self.condition:
            if self.mode == "collect":
                self._schedule(now)
            for d in DEVICES:
                m, p = self.motion[d], self.devices[d]
                if self.auto[d]:
                    self._autopilot(d, m, p, dt, now)
                was_moving = bool(m["v"] or m["s"] or m["w"])
                if (m["move"] or m["strafe"] or m["turn"]) and now - m["input_at"] > MOTION["input_timeout_s"]:
                    m["move"] = m["strafe"] = m["turn"] = 0  # the page stopped repeating: treat as released
                k = m.get("speed", 1.0)
                tv = k * (MOTION["forward_mm_s"] * m["move"] if m["move"] > 0 else MOTION["reverse_mm_s"] * m["move"] if m["move"] < 0 else 0.0)
                ts = k * MOTION["strafe_mm_s"] * m["strafe"]
                tw = k * MOTION["turn_deg_s"] * m["turn"]
                m["v"] = _approach(m["v"], tv, k * MOTION["accel_mm_s2"], k * MOTION["decel_mm_s2"], dt)
                m["s"] = _approach(m["s"], ts, k * MOTION["accel_mm_s2"], k * MOTION["decel_mm_s2"], dt)
                m["w"] = _approach(m["w"], tw, k * MOTION["turn_accel_deg_s2"], k * MOTION["turn_decel_deg_s2"], dt)
                if m["w"]:
                    p["heading"] = round((p["heading"] + m["w"] * dt) % 360.0, 3)
                    changed = True
                if m["v"] or m["s"]:
                    # forward along the heading (sin h, cos h), sideways to its right (cos h, −sin h)
                    h = math.radians(p["heading"])
                    dx = (m["v"] * math.sin(h) + m["s"] * math.cos(h)) * dt
                    dy = (m["v"] * math.cos(h) - m["s"] * math.sin(h)) * dt
                    self._move(p, m, dx, dy)
                    changed = True
                elif not (m["move"] or m["strafe"]):
                    m["blocked"] = m["sliding"] = None
                if was_moving and not (m["v"] or m["s"] or m["w"]):
                    changed = True  # came to rest this tick: publish / save the resting pose (final)
                if m["v"] or m["s"] or m["w"] or m["move"] or m["strafe"] or m["turn"]:
                    resting = False
            people_moved = self._step_people(dt) if self.people else False
            self._people_dirty = self._people_dirty or people_moved
            if changed:
                self.revision += 1
                self.version += 1
                self.origin, self.seq, self.final = "motion", None, resting
                self._dirty_at = None if resting else (self._dirty_at or time.monotonic())
                self.condition.notify_all()
            elif (people_moved or self._people_dirty) and now - self._people_pub_at >= 1.0 / PEOPLE_MOTION["publish_hz"]:
                # people are not saved: only tell the pages (no revision / file write); a change that came between two
                # publish slots is sent in the next one (else a person who just stopped kept its walking pose / old heading)
                self._people_pub_at, self._people_dirty = now, False
                self.version += 1
                self.condition.notify_all()
        if changed and resting:
            self._write()
        return changed

    def _mover(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            with self.condition:
                if not self._active():
                    self.condition.wait_for(lambda: self._active() or self._stop.is_set(), timeout=1.0)
                    last = time.monotonic()
                    continue
            self._stop.wait(1.0 / MOTION["tick_hz"])
            now = time.monotonic()
            self.step(min(0.1, now - last), now)
            last = now

    # --- API ---------------------------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self.condition:
            motion = {d: {"v": round(m["v"], 1), "s": round(m["s"], 1), "w": round(m["w"], 2), "move": m["move"], "strafe": m["strafe"],
                          "turn": m["turn"], "moving": bool(m["v"] or m["s"] or m["w"]), "blocked": m["blocked"], "sliding": m["sliding"]}
                      for d, m in self.motion.items()}
            snap = {"type": "state", "version": self.version, "revision": self.revision,
                    "devices": {d: {k: round(v, 1) for k, v in p.items()} for d, p in self.devices.items()},
                    "motion": motion, "motion_config": dict(MOTION), "drive_repeat_s": DRIVE_REPEAT_S, "auto": dict(self.auto), "rider": dict(self.rider), "mode": self.mode, "start": {d: dict(v) for d, v in self.start.items()},
                    "auto_state": {d: {"phase": a.get("phase"), "left_s": round(max(0.0, a.get("left", 0.0)), 1)} for d, a in self._auto.items() if self.auto[d]},
                    "hyeol": self._hyeol_state(),
                    "layout": copy.deepcopy(self.venue.layout) if self.venue is not None else None, "layout_revision": self.layout_revision,
                    "people": [{"id": p["id"], "x": round(p["x"], 1), "y": round(p["y"], 1), "heading": round(p["heading"], 1),
                                "v": round(p["v"], 1), "radius": p["r"], "height": p["h"], "evading": p["evading"], "cornered": p["cornered"],
                                "held": bool(p.get("held")), "away": p.get("away") is not None or bool(p.get("riding")), "riding": p.get("riding"),
                                # the silhouette parts placed in the world: exactly what the ToF mock traces
                                "parts": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in part.items()}
                                          for part in person_parts(p["x"], p["y"], p["heading"], p["r"], p["h"])]}
                               for p in self.people],
                    "names": dict(DEVICE_NAMES), "bounds": self.bounds(),
                    "venue": self.venue.info() if self.venue is not None else
                    {"id": "testroom", "name": "test room (procedural mock)", "status": "test fixture", "ceiling_known": True},
                    "tof_pose": self._tof_pose(self.devices["hyeol"], SIM_TOF_BACK),
                    "fixed": {"hyeol": {"tof_height_mm": SIM_TOF_H, "tof": "backward", "tof_back_mm": SIM_TOF_BACK, "distance_height_mm": 2000, "distance": "up"},
                              "maek": {"microphone": "separate pose"}},
                    "source": self.source, "origin": self.origin, "seq": self.seq, "final": self.final,
                    "saved": self.saved_revision == self.revision, "save_error": self.save_error, "path": str(self.path.name)}
            if self.venue is None:
                snap["room"] = copy.deepcopy(ROOM)
            return snap

    def _hyeol_state(self) -> dict:
        return {"attached": self.hyeol_attached, "state": "in_use" if self.hyeol_attached else "idle",
                "label": "사용 중" if self.hyeol_attached else "아이들링", "collecting": self.hyeol_attached,
                "simulated": True, "note": "시뮬레이션용 전환 — 실제 기구의 접속 판별 신호는 미결"}

    def hyeol_state(self) -> dict:
        with self.condition:
            return self._hyeol_state()

    def pose(self, device: str) -> dict:
        with self.condition:
            return dict(self.devices[device])

    def tof_pose(self) -> dict:
        """혈's ToF on the plan (2026-11-06, user): at the back of the device (SIM_TOF_BACK_MM behind its centre), looking
        backward. Every ToF consumer (mock camera, 공간선, 평면 흔적, /sim, /venue3d) uses this, not the device pose."""
        from mock_tof import SIM_TOF_BACK_MM
        with self.condition:
            p = self.devices["hyeol"]
            return self._tof_pose(p, SIM_TOF_BACK_MM)

    @staticmethod
    def _tof_pose(p: dict, back: float) -> dict:
        h = math.radians(p["heading"])
        return {"x": round(p["x"] - math.sin(h) * back, 1), "y": round(p["y"] - math.cos(h) * back, 1), "heading": round((p["heading"] + 180.0) % 360.0, 1)}

    def _apply_person(self, op: dict, origin: str | None, seq: int | None) -> dict:
        """{"op": "person", "id", "held": bool, "set"?: {x?, y?, heading?, v?}} (2026-11-06, /venue3d 관객 조종): a page takes over
        one mock-up person (held: it stops wandering and moves only by these ops, so the ToF mock sees it where the page
        walks it) or lets it go (held false: back to wandering from where it stands). Not saved (people are not)."""
        p = next((q for q in self.people if q["id"] == op.get("id")), None)
        if p is None:
            raise ValueError(f"person: no person {op.get('id')!r}")
        st = op.get("set") or {}
        if not isinstance(st, dict) or any(k not in ("x", "y", "heading", "v") or isinstance(v, bool) or not isinstance(v, (int, float)) or v != v
                                           for k, v in st.items()):
            raise ValueError("person: set must be {x?, y?, heading?, v?} numbers")
        with self.condition:
            if "held" in op:
                p["held"] = bool(op["held"])
                if not p["held"]:
                    p["tx"] = p["ty"] = None
                    p["v"], p["evading"], p["cornered"] = 0.0, False, False
            if "x" in st and "y" in st and self.venue is not None:
                b = self.venue.bounds
                st = {**st, "x": min(max(float(st["x"]), b["x0"]), b["x1"]), "y": min(max(float(st["y"]), b["y0"]), b["y1"])}
            for k, v in st.items():
                p[k] = float(v) % 360.0 if k == "heading" else float(v)
            self.revision += 1
            self.version += 1
            self.origin, self.seq = origin, seq
            self.condition.notify_all()
        return self.snapshot()

    def apply(self, op: object, final: bool, origin: str | None = None, seq: int | None = None) -> dict:
        """{"op": "drive", "device", "move", "turn", "strafe"?: -1|0|1 (buttons) or -1…1 (joystick), see module doc},
        {"op": "attach", "device": "hyeol", "attached": bool} (혈 in use / idling, simulation switch),
        {"op": "pose", "device", "set": {x?, y?, heading?}} (direct, for tests / tools) or {"op": "reset", "device"},
        {"op": "layout", "set": {big_walls?, small_walls?, windows?}} or {"op": "layout", "reset": true} (exhibition layout,
        venue.py; a layout that would hit 혈 / 맥 is refused, people standing in a new piece go back to their start)."""
        if isinstance(op, dict) and op.get("op") == "layout":
            return self._apply_layout(op, origin, seq)
        if isinstance(op, dict) and op.get("op") == "person":
            return self._apply_person(op, origin, seq)
        if isinstance(op, dict) and op.get("op") == "mode":   # {"op": "mode", "mode": null | "collect" | "learn" | "replay"}
            if op.get("mode") not in (None, "collect", "learn", "replay"):
                raise ValueError('mode: null, "collect", "learn" or "replay"')
            with self.condition:
                self.mode = op["mode"]
                self._dirty_at = self._dirty_at or time.monotonic()   # saved (kept over a restart)
                now = time.monotonic()
                for q in self.people:
                    q["board"] = None
                for d in DEVICES:
                    if self.mode in ("learn", "replay"):   # parked first, so a rider steps off behind the parking spot (not out where it was)
                        self.devices[d].update(self.start[d])
                    if any(q.get("riding") == d for q in self.people):
                        self._unboard(d)
                for d in DEVICES:
                    if self.mode == "collect":
                        self._next_session[d] = now   # 수집 starts: a visitor heads for each device at once (user: "2명은 바로")
                    elif self.auto[d] or self.rider[d]:   # any other mode: the sessions stop
                        self.auto[d] = self.rider[d] = False
                        self.motion[d].update(move=0, turn=0, strafe=0)
                    if self.mode in ("learn", "replay"):   # … and the devices are at their parking spots at once (user)
                        self.auto[d] = self.rider[d] = False
                        self.devices[d].update(self.start[d])
                        self.motion[d].update(move=0, turn=0, strafe=0, v=0.0, s=0.0, w=0.0)
                        self.revision += 1
                self.version += 1
                self.condition.notify_all()
            return self.snapshot()
        if isinstance(op, dict) and op.get("op") in ("board", "unboard"):
            # a page's visitor gets on / off a device (/venue3d 관객 조종, E key, user 2026-11-06):
            # {"op": "board", "id": person id | null, "device"} — on board (hidden), the device is the page's to drive;
            # {"op": "unboard", "device"} — only on the parking box: the device settles on its parking pose, the visitor
            # steps off behind it
            d = op.get("device")
            if d not in DEVICES:
                raise ValueError("board: device")
            with self.condition:
                if op["op"] == "board":
                    if any(q.get("riding") == d for q in self.people):
                        if not op.get("take"):
                            raise ValueError("board: someone is already on board")
                        self._unboard(d)   # 뺏기 (user 2026-11-07): the visitor riding it steps off by it, the page's visitor gets on
                    q = next((x for x in self.people if x["id"] == op.get("id")), None)
                    for o in self.people:
                        if o.get("board") == d:
                            o["board"] = None
                    self.auto[d], self.rider[d] = False, True
                    self.motion[d].update(move=0, turn=0, strafe=0, v=0.0, s=0.0, w=0.0)
                    if q is not None:
                        q["riding"], q["board"], q["item"] = d, None, None
                        q["x"], q["y"], q["v"], q["tx"], q["ty"] = -1e6, -1e6, 0.0, None, None
                else:
                    h, p = self.start[d], self.devices[d]
                    if math.hypot(p["x"] - h["x"], p["y"] - h["y"]) > 1300:
                        raise ValueError("unboard: only on the parking box")
                    p.update(h)
                    self.motion[d].update(move=0, turn=0, strafe=0, v=0.0, s=0.0, w=0.0)
                    self._unboard(d)
                self.revision += 1
                self.version += 1
                self.condition.notify_all()
            return self.snapshot()
        if isinstance(op, dict) and op.get("op") == "auto":   # {"op": "auto", "device", "on": bool} (관람중)
            if op.get("device") not in DEVICES or not isinstance(op.get("on"), bool):
                raise ValueError("auto: device and on (bool) required")
            with self.condition:
                d = op["device"]
                self.auto[d] = self.rider[d] = op["on"]
                self._auto[d].update(tx=None, ty=None, wait=0.0, pause_until=0.0, blocked_s=0.0, head=None, stuck_s=0.0,
                                     phase="roam", left=AUTO["roam_s"], meander=self._auto_rng.uniform(0, 6.3))
                self.motion[d].update(move=0, turn=0, strafe=0, v=0.0, s=0.0, w=0.0)
                # the session ends back where it started (user: no jump at the start); stopping leaves it where it is
                self._auto[d]["home"] = dict(self.devices[d])
                self.version += 1
                self.condition.notify_all()
            return self.snapshot()
        if not isinstance(op, dict) or op.get("device") not in DEVICES:
            raise ValueError(f"device must be one of {list(DEVICES)}")
        d = op["device"]
        kind = op.get("op")
        if kind == "attach":
            if d != "hyeol" or not isinstance(op.get("attached"), bool):
                raise ValueError('attach: device must be "hyeol" and attached true or false')
            with self.condition:
                self.hyeol_attached = op["attached"]
                self.revision += 1
                self.version += 1
                self.origin, self.seq, self.final = origin, seq, True
                self._dirty_at = None
                self.condition.notify_all()
            self._write()
            return self.snapshot()
        if kind == "drive":
            move, turn, strafe = op.get("move", 0), op.get("turn", 0), op.get("strafe", 0)  # strafe optional (older pages)
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not -1 <= v <= 1 for v in (move, turn, strafe)):
                raise ValueError("drive: move, turn and strafe must be numbers from -1 to 1")
            move, turn, strafe = (round(float(v), 3) if v not in (-1, 0, 1) else v for v in (move, turn, strafe))
            speed = op.get("speed", 1.0)  # optional speed factor (user 2026-11-06 "속도 조정"): top speeds and accelerations × speed
            if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed != speed or not 0.25 <= speed <= 4:
                raise ValueError("drive: speed must be a number from 0.25 to 4")
            with self.condition:
                m = self.motion[d]
                # stamped on the later of the real clock and the step clock (tests step faster than real time)
                stamp = max(time.monotonic(), self._now)
                if move or turn or strafe:
                    self._manual_at[d] = stamp
                if self.auto[d] and (move or turn or strafe):   # a hand on it (user): the session stops, the rider stays
                    self.auto[d] = False
                m.update(move=move, turn=turn, strafe=strafe, speed=float(speed), input_at=stamp)
                self.origin, self.seq = origin, seq
                # released while already standing still (e.g. stopped by a wall): the resting pose is final now
                settle = not (move or turn or strafe) and not (m["v"] or m["s"] or m["w"]) and not self.final
                if settle:
                    m["blocked"] = m["sliding"] = None
                    self.final, self._dirty_at = True, None
                    self.version += 1
                self.condition.notify_all()
            if settle:
                self._write()
            return self.snapshot()
        if kind == "pose":
            fields = self._v_pose(op.get("set"), partial=True, current=self.pose(d))
            if self.auto[d]:   # moved by hand / a 시안: the 관람중 session stops (the rider stays) and its last
                self.auto[d] = False   # steering input must not keep driving the device off the new spot
                self.motion[d].update(move=0, turn=0, strafe=0, v=0.0, s=0.0, w=0.0)
        elif kind == "reset":
            fields = dict(self.start[d])
        else:
            raise ValueError("op must be drive, attach, pose or reset")
        with self.condition:
            self.devices[d].update(fields)
            self.motion[d].update(v=0.0, s=0.0, w=0.0, move=0, strafe=0, turn=0, blocked=None, sliding=None)
            self.revision += 1
            self.version += 1
            self.origin, self.seq, self.final = origin, seq, final
            self._dirty_at = None if final else (self._dirty_at or time.monotonic())
            self.condition.notify_all()
        if final:
            self._write()
        return self.snapshot()

    def _apply_layout(self, op: dict, origin: str | None, seq: int | None) -> dict:
        if self.venue is None:
            raise ValueError("layout: the test room has no exhibition layout")
        new = self.venue.default_layout() if op.get("reset") is True else self.venue.check_layout(op.get("set"))
        if op.get("reset") is True:
            new = self.venue.check_layout(new)
        with self.condition:
            old_start = copy.deepcopy(self.start)
            self.venue.set_layout(new)   # no collision check (user 2026-11-06)
            preset = isinstance(op.get("set"), dict) and "parking" in op["set"]   # a 시안 applied: both onto their parking boxes
            for d, h in self.venue.start.items():   # parking boxes follow their walls (user 2026-11-07): so does a device parked there
                if d not in self.start or (h == old_start.get(d) and not preset):
                    continue
                self.start[d] = dict(h)
                q, o = self.devices[d], old_start[d]
                if (preset or math.hypot(q["x"] - o["x"], q["y"] - o["y"]) < 150) and not self.auto.get(d) and not self.rider.get(d):
                    q.update(h)
                    self.motion[d].update(move=0, turn=0, strafe=0, v=0.0, s=0.0, w=0.0)
            for p in self.people:  # a person inside a new piece steps back to their start (or stays if that is taken too)
                if self.venue.blocked(p["x"], p["y"], radius=p["r"], overhead=True):
                    cfg = next((c for c in self.venue.people["list"] if c["id"] == p["id"]), None) if self.venue.people else None
                    if cfg and not self.venue.blocked(cfg["start"][0], cfg["start"][1], radius=p["r"], overhead=True):
                        p.update(x=float(cfg["start"][0]), y=float(cfg["start"][1]), tx=None, ty=None, v=0.0)
            self.layout_revision += 1
            self.revision += 1
            self.version += 1
            self.origin, self.seq, self.final = origin, seq, True
            self._dirty_at = None
            self.condition.notify_all()
        self._write()
        return self.snapshot()

    def wait_change(self, last_version: int, timeout: float) -> int:
        with self.condition:
            self.condition.wait_for(lambda: self.version != last_version or self._stop.is_set(), timeout=timeout)
            return self.version
