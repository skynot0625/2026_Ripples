#!/usr/bin/env python3
"""Checks for the venue geometry, the venue ToF / distance mock and the simulator motion (no server, no browser).

    python venue_check.py [--venue m30]

1. geometry against the official drawing (grid, size, ceiling, openings, partition panels)
2. sensors in the venue: ToF centre distance to known faces, straight-up distance
3. motion: fixed speeds, gentle acceleration / deceleration, release coasting, collision (stop at contact, turning
   still allowed), the other device untouched, a page that stops repeating its input
4. saving: resting pose written with the venue id, restored by a new store; a file of another venue is not used
5. mock-up people in the ToF: front / side / near the lens / field edge / behind, against tracing every person on every
   ray, and the frame time with people beside and behind the camera (the 2026-10-14 Windows slowdown)
Prints one PASS / FAIL line per check, exits 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from mock_tof import PERSON, SIM_TOF_HEIGHT, MockToF
from simulator_store import MOTION, SimulatorStore, person_parts
from venue import DEVICE_RADIUS_MM, load_venue
import venue as venue_mod

venue_mod.USE_DEFAULT_PRESET = False   # fixed test poses below assume the geometry's default layout (check_presets covers the preset)

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))


def solid(v, sid):
    return next(s for s in v.solids if s["id"] == sid)


def centre_mm(venue, x, y, heading, frames=12):
    cam = MockToF(seed=3, venue=venue)
    cam.set_pose(x, y, heading, SIM_TOF_HEIGHT, 0.0)
    vals = []
    for i in range(frames):
        d, c = cam.frame(i / 30)
        patch = d[88:93, 118:123]
        patch = patch[patch > 0]
        if patch.size >= 6:
            vals.append(float(np.median(patch)))
    return float(np.median(vals)) if vals else float("nan"), cam


def check_m30_geometry(v) -> None:
    cols = {s["id"]: s["poly"] for s in v.solids if s["kind"] == "column" and s["id"].startswith("col-")}   # beams / brackets (photos) apart
    centre = {k: p.mean(axis=0) for k, p in cols.items()}
    span_x = centre["col-X5Y7"][0] - centre["col-X3Y7"][0]
    span_y = centre["col-X3Y3"][1] - centre["col-X3Y7"][1]
    # Y3 (north) columns pulled out of the north wall (user 2026-11-06): their centres 200 mm south of the grid line
    check(abs(span_x - 11400) <= 10 and abs(span_y - 19600) <= 10, "M30 grid X3–X5 × Y3–Y7 = official 11,400 × 19,800 (north columns pulled 200 forward)",
          f"{span_x:.0f} × {span_y:.0f} mm (column centres)")
    check(v.ceiling_z == 4200 and v.up_distance(3000, 6000, 2000) == 2200 and v.up_distance(550, 3000, 2000) == 900, "ceiling = official 4.2 m; up distance from 2.0 m = 2.2 m (away from the mock-up), 0.9 m under the H-beam (photos, 2.9 m assumed)",
          f"ceiling {v.ceiling_z}, up {v.up_distance(3000, 6000, 2000)}")
    check(len(cols) == 9, "9 columns from A-313", ", ".join(sorted(cols)))
    w = lambda p: p[:, 0].max() - p[:, 0].min()
    sizes = sorted(round(w(p)) for p in cols.values())
    check(sum(abs(z - 500) <= 3 for z in sizes) == 7 and sum(abs(z - 600) <= 3 for z in sizes) == 2, "column sizes 500 × 7, 600 × 2 (±3 mm drawing)", str(sizes))
    d1, d2 = solid(v, "glass-door-south-1")["poly"], solid(v, "glass-door-south-2")["poly"]
    check(abs(w(d1) - 4001) <= 2 and abs(w(d2) - 4000) <= 2, "main entrance: two 4,000 glass doors (A-313 1,450 | 4,000 | 500 | 4,000 | 1,450)",
          f"{w(d1):.0f}, {w(d2):.0f}; X4 column {w(cols['col-X4Y7']):.0f}")
    door = solid(v, "door-north")["poly"]
    check(abs(door[:, 0].min() - 1250) <= 1 and abs(w(door) - 1000) <= 1, "north door at 1,250, width 1,000 (A-313 top dims)")
    panels = [s for s in v.solids if s["id"].startswith("lobby-window-")]
    gl = max(s["poly"][:, 1].max() for s in panels) - min(s["poly"][:, 1].min() for s in panels)
    check(len(panels) == 10 and abs(gl - 12 * 1050) <= 5 and v.blocked(-500, 279 + 1050 * 1.5, radius=10) is None,
          "hall side: 12 bays × 1,050 (A-313), 10 sliding panels, bays 2–3 empty by default = entrance (user 2026-11-06)", f"{len(panels)} panels over {gl:.0f} mm")
    movable = [s for s in v.solids if s["kind"] == "movable_wall" and not s["id"].endswith("-foot")]
    lens = sorted(round(max(np.ptp(s["poly"][:, 0]), np.ptp(s["poly"][:, 1]))) for s in movable)
    thick = sorted(round(min(np.ptp(s["poly"][:, 0]), np.ptp(s["poly"][:, 1]))) for s in movable)
    check(lens == [5860, 6160, 6160, 6360] and thick == [84, 84, 84, 87], "4 big walls THK86: A-313 6,500 ×2, east-south 5,900 and west 6,300 (shortened 2026-11-06 so they fold to 90° without overlaps), all ending clear of the next mast, hinges on the mast centre lines (2026-11-07)", f"{lens}, thickness {thick}")
    inner_x = solid(v, "curtain-east")["poly"][:, 0].min() - solid(v, "curtain-west-north")["poly"][:, 0].max()
    check(abs(inner_x - 11370) <= 2, "curtains moved to the windows (user 2026-11-06): west curtain face → east curtain line", f"{inner_x:.0f} mm")


EXISTING_M30 = ["col-X3Y3", "col-X5Y3", "col-X3Y5", "col-X5Y5", "col-X3Y6", "col-X5Y6", "col-X3Y7", "col-X4Y7", "col-X5Y7", "wall-north-1", "door-north",
                "wall-north-2", "curtain-west-north", "curtain-east", "wall-south-1", "glass-door-south-1", "glass-door-south-2", "wall-south-2"]
NEW_M30 = ["door-north-head", "south-window-head", "south-soffit"]
LOBBY_M30 = ["lobby-core-west", "lobby-elevator-wall", "lobby-info-block", "lobby-partition-end", "lobby-glass-sw", "lobby-glass-se",
             "vestibule-wall-w", "vestibule-wall-e", "vestibule-door-inner", "vestibule-door-outer", "lobby-bench",
             "beam-west", "beam-east"] + [f"bracket-{c}" for c in ("X3Y7", "X3Y6", "X3Y5", "X3Y3", "X5Y7", "X5Y6", "X5Y5", "X5Y3")]
LAYOUT_M30 = ["movable-west", "movable-west-foot", "movable-east-1", "movable-east-1-foot", "movable-east-2", "movable-east-2-foot",
              "movable-east-3", "movable-east-3-foot", "sim-pc"] + [f"lobby-window-{i}" for i in range(1, 11)]   # feet: across each big wall's free end (user 2026-11-06)


def check_layout(v0) -> None:
    """Exhibition layout (2026-11-06): big walls rotate about the hinge, small walls anywhere free, windows open."""
    from venue import load_venue as lv
    v = lv("m30")
    st = v.check_layout({"big_walls": {"movable-west": 90}})
    v.set_layout(st)
    w = solid(v, "movable-west")["poly"]
    hx, hy, L = 985, 13201, 6100   # the hinge on the mast's centre line, 100 out from its tip (user 2026-11-07)
    check(abs(w[:, 0].min() - (hx - 60)) <= 1 and abs(w[:, 0].max() - (hx + L)) <= 1 and abs(w[:, 1].mean() - hy) <= 1,
          "big wall at 90°: swung about its hinge (985, 13,201) into the room, length kept", f"x {w[:, 0].min():.0f}–{w[:, 0].max():.0f}, y {w[:, 1].mean():.0f}")
    st = v.check_layout({"big_walls": {"movable-west": 180}})
    v.set_layout(st)
    w = solid(v, "movable-west")["poly"]
    check(abs(w[:, 1].min() - (13201 - 6100)) <= 1, "west big wall at 180° (drawing arc): lies south of the hinge", f"y {w[:, 1].min():.0f}–{w[:, 1].max():.0f}")
    bad = []
    for lay in ({"big_walls": {"movable-west": 200}}, {"windows": [1, 2, 3]}, {"windows": [2, 1, 3, 4, 5, 6, 7, 8, 9, 10]},
                {"small_walls": [{"id": "x", "x": 0, "y": 0, "heading": 0}]}):
        try:
            v.check_layout(lay)
            bad.append(lay)
        except ValueError:
            pass
    check(not bad, "layout form checked: angle over 180°, wrong panel count, panels out of order, bad small wall id (no collision check, user 2026-11-06)", str(bad))
    v.set_layout(v.check_layout({"windows": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}))
    check(v.blocked(-500, 279 + 1050 * 10.5, radius=10) is None and v.blocked(-500, 279 + 1050 * 2.5, radius=10) == "lobby-window-3",
          "sliding panels moved along the track: bays 11–12 open, bay 3 now closed", "")
    v.set_layout(v.check_layout({"small_walls": [{"id": "small-1", "x": 5500, "y": 9970, "heading": 0}]}))
    check(any(s_["id"] == "small-1" for s_ in v.solids), "a small wall may be placed anywhere (even into 심): no collision check")
    v.set_layout(v.check_layout({"small_walls": [{"id": "small-1", "x": 3000, "y": 3000, "heading": 0}]}))
    panel = solid(v, "small-1")
    check(panel["z0"] == 100 and panel["z1"] == 2545 and abs(np.ptp(panel["poly"][:, 0]) - 2445) <= 1 and v.blocked(3000, 3000) == "small-1",
          "small wall 2,445 × 2,445 on 100 mm casters, faces heading 0 (panel along x), blocks devices", f"z {panel['z0']}–{panel['z1']}")
    tof_before = v.edges.shape[0]
    v.set_layout(v.default_layout())
    check(v.edges.shape[0] < tof_before and v.blocked(3000, 3000) is None, "removing a small wall rebuilds collision and ToF faces", f"edges {tof_before} → {v.edges.shape[0]}")


def ray2d(v, x, y, heading):
    """Distance along the floor plan from (x, y) to the first solid face in that heading (inf = nothing at all)."""
    hx, hy = math.sin(math.radians(heading)), math.cos(math.radians(heading))
    best = math.inf
    for ax, ay, bx, by in v.edges[:, :4]:
        ex, ey = bx - ax, by - ay
        d = hx * ey - hy * ex
        if abs(d) < 1e-12:
            continue
        q, t = ((ax - x) * ey - (ay - y) * ex) / d, ((ax - x) * hy - (ay - y) * hx) / d
        if q > 1e-6 and 0 <= t <= 1 and q < best:
            best = q
    return best


def check_m30_boundary(v) -> None:
    """The temporary verification enclosure (2026-10-14 15:45) was removed by the user's decision; the gaps between the
    boundary structures are left open (not filled with anything else). Windows: all windows in A-313 are behind curtains."""
    layers = {s["layer"] for s in v.solids}
    check("verification_enclosure" not in layers and not any(s["id"].startswith("verify-enclosure") for s in v.solids),
          "verification enclosure removed (no verify-enclosure-* solids, no verification_enclosure layer)")
    others = [s["id"] for s in v.solids if s["layer"] != "exhibition_mockup"]
    check(others == EXISTING_M30 + LOBBY_M30 + NEW_M30 + LAYOUT_M30, "drawing structures unchanged + lobby (2009 drawings) + layout pieces (default layout)",
          f"{len(others)} solids")
    movable = {s["id"]: [float(s["poly"][:, 0].min()), float(s["poly"][:, 1].min()), float(s["poly"][:, 0].max()), float(s["poly"][:, 1].max())]
               for s in v.solids if s["kind"] == "movable_wall" and not s["id"].endswith("-foot")}
    want = {"movable-west": [941.5, 13141, 1028.5, 19301], "movable-east-1": [10372, 440, 10456, 6300],   # 60 past the hinge (hinge_overhang)
            "movable-east-2": [10372, 6540, 10456, 12900], "movable-east-3": [10372, 13141, 10456, 19301]}
    check(all(max(abs(a - b) for a, b in zip(movable[k], r)) <= 1 for k, r in want.items()) and len(movable) == 4,
          "default layout: the 4 big walls closed at the A-313 positions (west and east-south hinges moved in, 2026-11-06)", str(movable))
    check_layout(v)
    escapes = 0
    for x in np.arange(-150, 11000, 1000):
        for y in np.arange(400, 19500, 1000):
            if v.blocked(x, y):
                continue
            escapes += sum(math.isinf(ray2d(v, x, y, h)) for h in range(0, 360, 6))
    check(True, "record: boundary gaps are not filled with anything (rays leaving the space)", f"{escapes} rays (1 m grid, every 6°)")


def first_hit(v, x, y, heading):
    """Kind of the first solid face along the floor-plan ray (movable / glass / … / None)."""
    hx, hy = math.sin(math.radians(heading)), math.cos(math.radians(heading))
    best, kind = math.inf, None
    for s in v.solids:
        p = s["poly"]
        for i in range(len(p)):
            ax, ay = p[i]; bx, by = p[(i + 1) % len(p)]
            ex, ey = bx - ax, by - ay
            d = hx * ey - hy * ex
            if abs(d) < 1e-12:
                continue
            q, t = ((ax - x) * ey - (ay - y) * ex) / d, ((ax - x) * hy - (ay - y) * hx) / d
            if q > 1e-6 and 0 <= t <= 1 and q < best and s["z0"] <= 1500 and (s["z1"] is None or s["z1"] >= 1500):
                best, kind = q, ("movable" if s["kind"] == "movable_wall" else s["kind"])
    return kind


def check_m30_sensors(v) -> None:
    cases = [  # (x, y, heading, expected centre distance, what)
        (5000, 17000, 0, 19851 - 17000, "north wall"),
        (8000, 10000, 90, 10372 - 8000, "east big wall (closed; hinge 100 out from the mast, 2026-11-07)"),
        (1500, 6650, 270, 1500 - 349, "column X3Y6"),
        (1500, 16000, 270, 1500 - 1028.5, "west big wall (closed; hinge 100 out from the mast, 2026-11-07)"),
        (5700, 1500, 180, 1500 - 250, "column X4Y7 between the south glass doors"),
        (-4000, 10000, 0, 12800 - 10000, "lobby: 안내 block wall, from the hall (2009 drawings)"),
    ]
    for x, y, h, want, what in cases:
        got, cam = centre_mm(v, x, y, h)
        check(abs(got - want) <= 40, f"ToF centre at ({x}, {y}) facing {h}° = {what}", f"{got:.0f} mm (expected {want:.0f})")
    got, cam = centre_mm(v, 5000, 5000, 90)
    check(not math.isfinite(got), "beyond 4 m (east big wall 5.5 m away) reads invalid, never wrapped", f"centre {got}")
    labels = set(np.unique(cam.last_labels).tolist())
    check(1 in labels, "floor visible in the lower rows (ToF 1.5 m, level)", f"labels {sorted(labels)}")
    t0 = time.perf_counter()
    for i in range(20):
        cam.set_pose(4000 + i * 10, 9000, i * 3, SIM_TOF_HEIGHT, 0.0)
    trace_ms = (time.perf_counter() - t0) * 1000 / 20
    t0 = time.perf_counter()
    for i in range(30):
        cam.frame(i / 30)
    frame_ms = (time.perf_counter() - t0) * 1000 / 30
    check(trace_ms + frame_ms < 25, "pose change re-trace + frame < 25 ms (30 fps budget 33 ms)", f"re-trace {trace_ms:.1f} ms + frame {frame_ms:.1f} ms")


INSIDE = []  # positions found inside an obstacle during any run (must stay empty)


def run(store, device, move, turn, seconds, dt=1 / 30, strafe=0):
    """Hold an input for `seconds` (repeated like a page does), stepping the motion by dt."""
    op = {"op": "drive", "device": device, "move": move, "turn": turn, "strafe": strafe}
    store.apply(op, False)
    for _ in range(round(seconds / dt)):
        store.apply(op, False)
        store.step(dt, time.monotonic())
        p = store.pose(device)
        if store.blocked(p["x"], p["y"]) is not None:
            INSIDE.append((device, round(p["x"]), round(p["y"])))


def coast(store, device, seconds, dt=1 / 30):
    store.apply({"op": "drive", "device": device, "move": 0, "turn": 0}, False)
    for _ in range(round(seconds / dt)):
        store.step(dt, time.monotonic())


def check_motion(v, tmp: Path) -> None:
    s = SimulatorStore(tmp / "sim.json", v, mover=False, people=False)
    s.apply({"op": "pose", "device": "hyeol", "set": {"x": 2500, "y": 6000, "heading": 0}}, True)  # facing +y, open floor ahead
    p0, other0 = s.pose("hyeol"), s.pose("maek")
    run(s, "hyeol", 1, 0, 0.5)
    v_half = s.motion["hyeol"]["v"]
    run(s, "hyeol", 1, 0, 0.5)
    v_one = s.motion["hyeol"]["v"]
    run(s, "hyeol", 1, 0, 2.0)
    p1 = s.pose("hyeol")
    dist = math.hypot(p1["x"] - p0["x"], p1["y"] - p0["y"])
    check(abs(v_half - MOTION["accel_mm_s2"] * 0.5) <= 20 and abs(v_one - MOTION["forward_mm_s"]) <= 1,
          "forward: gentle acceleration, then fixed top speed", f"0.5 s: {v_half:.0f} mm/s, 1 s: {v_one:.0f} mm/s (top {MOTION['forward_mm_s']:.0f})")
    want = 0.5 * MOTION["accel_mm_s2"] * 1.0 ** 2 + MOTION["forward_mm_s"] * 2.0
    check(abs(dist - want) <= 25 and abs(p1["x"] - p0["x"]) < 1, "3 s held forward travels ½·a·1² + v·2 straight ahead", f"{dist:.0f} mm (expected {want:.0f})")
    before = s.pose("hyeol")["y"]
    t_stop = 0.0
    s.apply({"op": "drive", "device": "hyeol", "move": 0, "turn": 0}, False)
    while s.motion["hyeol"]["v"] and t_stop < 2:
        s.step(1 / 30, time.monotonic()); t_stop += 1 / 30
    glide = s.pose("hyeol")["y"] - before
    want_glide = MOTION["forward_mm_s"] ** 2 / (2 * MOTION["decel_mm_s2"])
    check(abs(t_stop - MOTION["forward_mm_s"] / MOTION["decel_mm_s2"]) <= 0.05 and abs(glide - want_glide) <= 15,
          "release: decelerates to a stop (slight coasting)", f"stopped after {t_stop:.2f} s, coasted {glide:.0f} mm (expected {want_glide:.0f})")
    check(s.pose("maek") == other0, "driving 혈 leaves 맥 unchanged", str(s.pose("maek")))

    run(s, "hyeol", -1, 0, 2.0)
    check(abs(s.motion["hyeol"]["v"] + MOTION["reverse_mm_s"]) <= 1, "backward: fixed top speed", f"{s.motion['hyeol']['v']:.0f} mm/s")
    coast(s, "hyeol", 1.0)
    h0 = s.pose("hyeol")["heading"]
    run(s, "hyeol", 0, 1, 2.0)
    w = s.motion["hyeol"]["w"]
    turned = (s.pose("hyeol")["heading"] - h0) % 360
    want_turn = 0.5 * MOTION["turn_accel_deg_s2"] * (MOTION["turn_deg_s"] / MOTION["turn_accel_deg_s2"]) ** 2 + MOTION["turn_deg_s"] * (2.0 - MOTION["turn_deg_s"] / MOTION["turn_accel_deg_s2"])
    check(abs(w - MOTION["turn_deg_s"]) <= 0.01 and abs(turned - want_turn) <= 1.5, "right turn (우회전): heading increases at the fixed turn speed",
          f"{w:.1f}°/s, turned {turned:.1f}° in 2 s (expected {want_turn:.1f})")
    coast(s, "hyeol", 1.0)
    h1 = s.pose("hyeol")["heading"]
    run(s, "hyeol", 0, -1, 1.0)
    coast(s, "hyeol", 1.0)
    check((h1 - s.pose("hyeol")["heading"]) % 360 > 10, "left turn (좌회전): heading decreases", f"{h1:.1f}° → {s.pose('hyeol')['heading']:.1f}°")

    # collision: face north and drive into the north wall
    s.apply({"op": "pose", "device": "hyeol", "set": {"x": 5000, "y": 18000, "heading": 0}}, True)
    run(s, "hyeol", 1, 0, 6.0)
    p = s.pose("hyeol")
    face = 19851
    check(abs(p["y"] - (face - DEVICE_RADIUS_MM)) <= 2 and s.motion["hyeol"]["v"] == 0 and s.motion["hyeol"]["blocked"] == "wall-north-2",
          "north wall stops the device at contact (radius 300), speed 0", f"y {p['y']:.0f} (wall face {face}), blocked by {s.motion['hyeol']['blocked']}")
    s.apply({"op": "drive", "device": "hyeol", "move": 0, "turn": 0}, False)  # release while stopped at the wall
    check(s.final and s.saved_revision == s.revision, "released while stopped by a wall: resting pose final and saved at once")
    h_before = p["heading"]
    run(s, "hyeol", 1, 1, 1.5)
    check((s.pose("hyeol")["heading"] - h_before) % 360 > 10, "blocked: turning still works", f"heading {h_before:.0f}° → {s.pose('hyeol')['heading']:.0f}°")
    coast(s, "hyeol", 1.0)
    # columns and glass
    # (the mast on X3Y6 reaches out to the hinge line from 2.0 m up, below the device height: it blocks first — user 2026-11-06)
    mast_x = float(next(x["poly"] for x in v.solids if x["id"] == "bracket-X3Y6")[:, 0].max())
    s.apply({"op": "pose", "device": "hyeol", "set": {"x": 2500, "y": 6650, "heading": 270}}, True)
    run(s, "hyeol", 1, 0, 6.0)
    check(s.motion["hyeol"]["blocked"] == "bracket-X3Y6" and abs(s.pose("hyeol")["x"] - (mast_x + DEVICE_RADIUS_MM)) <= 2, "column X3Y6 (its mast) blocks",
          f"x {s.pose('hyeol')['x']:.0f}, blocked by {s.motion['hyeol']['blocked']}")
    coast(s, "hyeol", 1.0)
    s.apply({"op": "pose", "device": "hyeol", "set": {"x": 1500, "y": 5400, "heading": 270}}, True)
    run(s, "hyeol", 1, 0, 6.0)
    check(s.motion["hyeol"]["blocked"] == "lobby-window-3" and s.pose("hyeol")["x"] > 0 and abs(s.pose("hyeol")["x"] - (-462 + DEVICE_RADIUS_MM)) <= 2,
          "west side: a closed sliding window to the lobby blocks (no walking through)", f"x {s.pose('hyeol')['x']:.0f}, blocked by {s.motion['hyeol']['blocked']}")
    coast(s, "hyeol", 1.0)
    s.apply({"op": "pose", "device": "hyeol", "set": {"x": 1500, "y": 279 + 1050 * 2, "heading": 270}}, True)
    run(s, "hyeol", 1, 0, 9.0)
    check(s.pose("hyeol")["x"] < -1500, "entrance (empty bays 2–3, 2,100 wide): 혈 drives through into the lobby", f"x {s.pose('hyeol')['x']:.0f}, blocked by {s.motion['hyeol']['blocked']}")
    coast(s, "hyeol", 1.0)
    s.apply({"op": "pose", "device": "hyeol", "set": {"x": 2500, "y": 6000, "heading": 0}}, True)
    s.apply({"op": "layout", "reset": True}, True)
    for x, y in ((5000, 50), (5700, 0), (11300, 9000), (-500, 2000), (-9000, 5000)):
        try:
            s.apply({"op": "pose", "device": "maek", "set": {"x": x, "y": y}}, True)
            ok = False
        except ValueError:
            ok = True
        check(ok, f"pose inside a solid / outside rejected ({x}, {y})")

    # a page that stops repeating its input (closed tab): the device stops by itself
    s.apply({"op": "pose", "device": "maek", "set": {"x": 2500, "y": 7500, "heading": 0}}, True)
    s.apply({"op": "drive", "device": "maek", "move": 1, "turn": 0}, False)
    t = time.monotonic()
    for i in range(30):
        s.step(1 / 30, t + i / 30)
    s.step(1 / 30, t + MOTION["input_timeout_s"] + 0.5)
    for i in range(30):
        s.step(1 / 30, t + 2 + i / 30)
    check(s.motion["maek"]["move"] == 0 and s.motion["maek"]["v"] == 0, "input not repeated within the timeout counts as released",
          f"move {s.motion['maek']['move']}, v {s.motion['maek']['v']}")
    for bad in ({"move": 2}, {"move": True}, {"turn": "1"}):
        try:
            s.apply(dict({"op": "drive", "device": "hyeol"}, **bad), False)
            ok = False
        except ValueError:
            ok = True
        check(ok, f"invalid drive input rejected {bad}")

    # saving / restore
    resting = s.pose("hyeol")
    saved = json.loads((tmp / "sim.json").read_text(encoding="utf-8"))
    check(saved.get("venue") == v.id and abs(saved["devices"]["hyeol"]["x"] - resting["x"]) < 0.1, "resting pose saved with the venue id",
          f"venue {saved.get('venue')}, x {saved['devices']['hyeol']['x']}")
    s2 = SimulatorStore(tmp / "sim.json", v, mover=False, people=False)
    same = lambda a, b: all(abs(a[k] - b[k]) <= 0.06 for k in ("x", "y", "heading"))
    check(same(s2.pose("hyeol"), resting) and same(s2.pose("maek"), s.pose("maek")),
          "new store (restart) restores both poses", f"혈 {s2.pose('hyeol')} / {resting}; 맥 {s2.pose('maek')} / {s.pose('maek')}")
    other = tmp / "other.json"
    other.write_text(json.dumps({"version": 2, "venue": "powerplant", "revision": 1, "devices": saved["devices"]}), encoding="utf-8")
    s3 = SimulatorStore(other, v, mover=False, people=False)
    asides = list(tmp.glob("other.json.venue-powerplant-*"))
    check(s3.pose("hyeol") == v.start["hyeol"] and asides, "a file saved for another venue is set aside, start poses used", f"{[a.name for a in asides]}")
    legacy = tmp / "legacy.json"
    legacy.write_text(json.dumps({"version": 1, "revision": 2, "devices": {"hyeol": {"x": 100, "y": 200, "heading": 0}, "maek": {"x": 0, "y": 0, "heading": 0}}}), encoding="utf-8")
    s4 = SimulatorStore(legacy, None, mover=False)
    check(s4.pose("hyeol")["x"] == 100, "test room still reads the old version-1 file (no venue field)")


def check_presets() -> None:
    """layout_presets.json (2026-11-06): listed in the API, the default one is the default layout and the start poses."""
    venue_mod.USE_DEFAULT_PRESET = True
    try:
        pv = load_venue("m30")
        default = pv.default_layout()   # read while the flag is on
    finally:
        venue_mod.USE_DEFAULT_PRESET = False
    pr = pv.layout_presets
    dp = next((q for q in pr if q.get("default")), None)
    check(len(pr) >= 2 and [q["name"] for q in pr[:2]] == ["시안 1", "시안 2"], "presets: 시안 1 · 시안 2 listed", str([q.get("name") for q in pr]))
    check(dp is not None and dp["name"] == "시안 1", "presets: 시안 1 is the default")
    check(dp is not None and default["sim"] == dp["layout"]["sim"] and pv.layout["windows"] == dp["layout"]["windows"],
          "presets: default layout = 시안 1 (심, 창)", str(pv.layout.get("sim")))
    want = pv.parking_starts(pv.layout)   # 2026-11-07: the parking boxes follow their screen walls
    check(dp is not None and set(want) == {"hyeol", "maek"} and all(abs(pv.start[d][k] - want[d][k]) < 0.01 for d in want for k in ("x", "y", "heading")),
          "presets: start poses = 시안 1 혈 / 맥 (from their screen walls)", str(pv.start))
    check(len(pv.view()["layout"]["presets"]) == len(pr), "presets: in the venue API (view)")


def check_slide(v, tmp: Path) -> None:
    """Oblique contact slides along the surface; head-on and concave corners stop; strafe keeps the heading."""
    s = SimulatorStore(tmp / "slide.json", v, mover=False, people=False)
    put = lambda d, x, y, h: s.apply({"op": "pose", "device": d, "set": {"x": x, "y": y, "heading": h}}, True)
    for dev in ("hyeol", "maek"):  # same rule for both devices
        other = "maek" if dev == "hyeol" else "hyeol"
        o0 = s.pose(other)
        put(dev, 5000, 18800, 30)
        run(s, dev, 1, 0, 6.0)
        p, m = s.pose(dev), s.motion[dev]
        NF = 19851 - DEVICE_RADIUS_MM
        contact_x = 5000 + (NF - 18800) * math.tan(math.radians(30))
        check(abs(p["y"] - NF) <= 2 and p["x"] > contact_x + 800 and m["v"] > 0 and m["sliding"] == "wall-north-2" and p["heading"] == 30,
              f"{dev}: oblique (30°) into the north wall slides along it, not through", f"y {p['y']:.0f} (face − r = {NF:.0f}), x {p['x']:.0f} (contact ≈ {contact_x:.0f}), v {m['v']:.0f}, sliding {m['sliding']}")
        coast(s, dev, 1.0)
        check(s.pose(other) == o0, f"{dev} sliding leaves the other device unchanged")
    put("hyeol", 5000, 18800, 0)
    run(s, "hyeol", 1, 0, 4.0)
    check(s.motion["hyeol"]["v"] == 0 and s.motion["hyeol"]["blocked"] == "wall-north-2" and abs(s.pose("hyeol")["x"] - 5000) < 1,
          "head-on (0°) into the north wall stops", f"x {s.pose('hyeol')['x']:.1f}, y {s.pose('hyeol')['y']:.0f}")
    coast(s, "hyeol", 1.0)
    put("hyeol", 5000, 18800, 5)
    run(s, "hyeol", 1, 0, 4.0)
    check(s.motion["hyeol"]["v"] == 0 and s.motion["hyeol"]["blocked"], f"nearly head-on (5° < {MOTION['slide_min_angle_deg']:.0f}°) stops too",
          f"x {s.pose('hyeol')['x']:.0f}")
    coast(s, "hyeol", 1.0)
    # column X3Y6 with its mast (end x = mast_x): heading 250° = west and a little south
    mast_x = float(next(x["poly"] for x in v.solids if x["id"] == "bracket-X3Y6")[:, 0].max())
    put("hyeol", 2500, 6700, 250)
    ys = []
    for _ in range(8):
        run(s, "hyeol", 1, 0, 0.5); ys.append(s.pose("hyeol")["y"])
    seen = s.motion["hyeol"]
    check(min(ys) < 6700 - 300 and s.pose("hyeol")["x"] < mast_x + DEVICE_RADIUS_MM + 2, "oblique into column X3Y6: slides along its mast's end and on", f"y {[round(y) for y in ys]}, x {s.pose('hyeol')['x']:.0f}")
    coast(s, "hyeol", 1.0)
    # closed sliding windows to the lobby (face x = −462): heading 240°
    put("hyeol", 1500, 12000, 240)
    run(s, "hyeol", 1, 0, 6.0)
    p, m = s.pose("hyeol"), s.motion["hyeol"]
    check(abs(p["x"] - (-462 + DEVICE_RADIUS_MM)) <= 2 and p["y"] < 12000 - 1000 and (str(m["sliding"]).startswith("lobby-window") or m["blocked"]),
          "oblique into the closed lobby windows: slides along them, not through", f"x {p['x']:.0f} (face + r = {-462 + DEVICE_RADIUS_MM:.0f}), y {p['y']:.0f}, sliding {m['sliding']}, blocked {m['blocked']}")
    coast(s, "hyeol", 1.0)
    # concave corner: north wall × closed east big wall (face x 10,472) — its foot bar (user 2026-11-06) sticks out across
    # the corner, so the device stops against the north wall and the foot's room-side, south corner
    put("hyeol", 8900, 18700, 45)
    run(s, "hyeol", 1, 0, 10.0)
    p, m = s.pose("hyeol"), s.motion["hyeol"]
    ft = next(x["poly"] for x in s.venue.solids if x["id"] == "movable-east-3-foot")
    fx, fy = float(ft[:, 0].min()), float(ft[:, 1].min())
    check(abs(p["y"] - (19851 - DEVICE_RADIUS_MM)) <= 3 and abs(math.hypot(p["x"] - fx, p["y"] - fy) - DEVICE_RADIUS_MM) <= 3 and m["v"] == 0 and m["blocked"],
          "concave corner (north wall × closed east big wall with its foot): stops against the wall and the foot", f"({p['x']:.0f}, {p['y']:.0f}), blocked {m['blocked']}")
    coast(s, "hyeol", 1.0)
    # strafe
    put("maek", 2500, 7000, 0)
    run(s, "maek", 0, 0, 0.3, strafe=1)
    s_mid = s.motion["maek"]["s"]
    run(s, "maek", 0, 0, 2.7, strafe=1)
    p = s.pose("maek")
    want = 0.5 * MOTION["accel_mm_s2"] * (MOTION["strafe_mm_s"] / MOTION["accel_mm_s2"]) ** 2 + MOTION["strafe_mm_s"] * (3.0 - MOTION["strafe_mm_s"] / MOTION["accel_mm_s2"])
    check(abs(s_mid - 150) <= 20 and abs(s.motion["maek"]["s"] - MOTION["strafe_mm_s"]) <= 1 and abs(p["x"] - 2500 - want) <= 25 and abs(p["y"] - 7000) < 1 and p["heading"] == 0,
          "우이동 (heading 0°): moves +x only, heading unchanged, gentle acceleration to the fixed sideways speed",
          f"0.3 s {s_mid:.0f} mm/s, top {s.motion['maek']['s']:.0f}, Δx {p['x'] - 2500:.0f} (expected {want:.0f}), Δy {p['y'] - 7000:.1f}")
    x_rel = s.pose("maek")["x"]
    t_stop = 0.0
    s.apply({"op": "drive", "device": "maek", "move": 0, "turn": 0, "strafe": 0}, False)
    while s.motion["maek"]["s"] and t_stop < 2:
        s.step(1 / 30, time.monotonic()); t_stop += 1 / 30
    glide = s.pose("maek")["x"] - x_rel
    check(abs(t_stop - MOTION["strafe_mm_s"] / MOTION["decel_mm_s2"]) <= 0.05 and abs(glide - MOTION["strafe_mm_s"] ** 2 / (2 * MOTION["decel_mm_s2"])) <= 10,
          "release sideways: same deceleration (slight coasting)", f"stopped after {t_stop:.2f} s, coasted {glide:.0f} mm")
    put("hyeol", 2500, 7000, 90)
    run(s, "hyeol", 0, 0, 2.0, strafe=-1)
    p = s.pose("hyeol")
    check(p["y"] > 7000 + 400 and abs(p["x"] - 2500) < 1 and p["heading"] == 90, "좌이동 (heading 90°): moves to the device's left (+y), heading unchanged", f"Δy {p['y'] - 7000:.0f}")
    coast(s, "hyeol", 1.0)
    put("hyeol", 5000, 19000, 90)
    run(s, "hyeol", 0, 0, 4.0, strafe=-1)
    check(abs(s.pose("hyeol")["y"] - (19851 - DEVICE_RADIUS_MM)) <= 2 and s.motion["hyeol"]["s"] == 0 and s.motion["hyeol"]["blocked"] and s.pose("hyeol")["heading"] == 90,
          "좌이동 head-on into the north wall: stops at contact, heading unchanged", f"y {s.pose('hyeol')['y']:.0f}")
    coast(s, "hyeol", 1.0)
    put("hyeol", 5000, 19000, 60)
    run(s, "hyeol", 0, 0, 4.0, strafe=-1)
    p = s.pose("hyeol")
    check(abs(p["y"] - (19851 - DEVICE_RADIUS_MM)) <= 2 and p["x"] < 5000 - 400 and s.motion["hyeol"]["sliding"] == "wall-north-2", "좌이동 at an angle (heading 60°): slides along the north wall",
          f"({p['x']:.0f}, {p['y']:.0f})")
    coast(s, "hyeol", 1.0)
    put("hyeol", 2500, 6000, 0)
    run(s, "hyeol", 1, 0, 2.0, strafe=1)
    p = s.pose("hyeol")
    check(p["x"] > 2800 and p["y"] > 6500 and p["heading"] == 0, "전진 + 우이동 together: diagonal, heading unchanged", f"({p['x']:.0f}, {p['y']:.0f})")
    coast(s, "hyeol", 1.0)
    check(not INSIDE, "never inside a wall / column / glass during any run", str(INSIDE[:5]))
    try:
        s.apply({"op": "drive", "device": "hyeol", "move": 0, "turn": 0, "strafe": 2}, False); ok = False
    except ValueError:
        ok = True
    check(ok, "invalid strafe rejected")
    s.apply({"op": "drive", "device": "hyeol", "move": 0, "turn": 0}, False)
    check(s.motion["hyeol"]["strafe"] == 0, "drive without strafe (older pages) still accepted, strafe 0")


def check_mockup(v, tmp: Path) -> None:
    """Exhibition mock-up layer (venues/m30/mockup.json): 심 = the /sim model (2026-11-06), no blinds; verification values."""
    mock = [s for s in v.solids if s["layer"] == "exhibition_mockup"]
    raw = {s["id"]: s["raw"] for s in v.solids}
    check(mock and all(s["status"] == "검증용 목업 (설치 확정값 아님)" and "make_mockup.py" in raw[s["id"]]["ref"] for s in mock)
          and all(s["layer"] != "exhibition_mockup" for s in v.solids[:23]),
          "mock-up solids live in their own layer file, every one marked 검증용 목업 (설치 확정값 아님)", f"{len(mock)} solids in mockup.json")
    leds = [s for s in mock if s["kind"] == "mock_led"]; plates = [s for s in mock if s["kind"] == "mock_plate"]
    frames = [s for s in mock if s["kind"] == "mock_frame"]
    bottoms, tops = [x["z0"] + 15 for x in leds], [x["z1"] for x in leds]
    check(len(leds) == 180 and min(bottoms) >= 49 and max(bottoms) <= 100 and abs(min(tops) - 2250) <= 2 and abs(max(tops) - 3000) <= 2,
          "심 = /sim model lowered for M30 (user 2026-11-06): 180 strips, highest hole 3.0 m (row A), row Q 2.25 m, ends cut 5–6 LEDs to stay 5–10 cm above the floor",
          f"{len(leds)} strips, ends {min(bottoms):.0f}–{max(bottoms):.0f}, tops {min(tops):.0f}–{max(tops):.0f} mm")
    ya = np.mean([x["poly"][:, 1].mean() for x in leds if x["id"].startswith("led-A")]); yq = np.mean([x["poly"][:, 1].mean() for x in leds if x["id"].startswith("led-Q")])
    ta = max(x["z1"] for x in leds if x["id"].startswith("led-A")); tq = max(x["z1"] for x in leds if x["id"].startswith("led-Q"))
    check(len(plates) == 17 and ya > yq and ta > tq, "tilted plate stored as 17 row bands; row A is north (+y) and higher than row Q",
          f"A y {ya:.0f} at {ta:.0f} mm, Q y {yq:.0f} at {tq:.0f} mm")
    xs = np.concatenate([p_["poly"][:, 0] for p_ in plates]); ys = np.concatenate([p_["poly"][:, 1] for p_ in plates])
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    check(abs(np.ptp(xs) - 2120) <= 2 and abs(np.ptp(ys) - 1835.97) <= 2 and abs(cy - (90 + 19851) / 2) < 50,
          "plate 2,120 × 1,835.97 (six 1,060 triangles) in the middle of the gallery", f"{np.ptp(xs):.0f} × {np.ptp(ys):.0f}, centre ({cx:.0f}, {cy:.0f})")
    blinds = [s for s in v.solids if s["kind"] == "mock_blind"]
    check(not blinds and any(f["id"].startswith("sim-leg") for f in frames), "no blinds (2026-11-06); tower legs present", f"{len(frames)} frame pieces")
    dividers = [s["id"] for s in v.solids if s["kind"] in ("wall", "movable_wall", "glass", "curtain") and s["layer"] != "layout"
                and s["poly"][:, 1].min() < cy < s["poly"][:, 1].max() and np.ptp(s["poly"][:, 0]) > 2000 and s["poly"][:, 0].min() > -400]
    check(not dividers, "no wall between the two halves of the gallery", str(dividers))
    # ToF sees the strips; the plate from below; up distance under the plate
    got, cam = centre_mm(v, cx, cy - 2000, 0)
    share = float((cam.last_labels == 22).mean())
    check(share > 0.1, "ToF: 심's LED strips seen ahead from 2 m (row Q side; the middle of 심 has no strips)", f"strip pixels {share:.0%}")
    got, cam = centre_mm(v, cx - 2300, cy, 90)
    check(21 in set(np.unique(cam.last_labels[:60]).tolist()), "ToF: the top plate is seen from below (upper rows)")
    under = v.up_distance(cx + 800, cy, 2000)
    check(under is not None and 200 < under < 1800 and v.up_distance(3000, 13000, 2000) == 2200,
          "up distance: under the tilted plate shorter than the 2.2 m elsewhere", f"{under} mm under the plate")
    check(v.blocked(cx, cy - 700) is not None and v.blocked(cx, cy - 2500) is None, "collision: 심's strips block a device; 2.5 m away is free",
          f"{v.blocked(cx, cy - 700)}")
    # people
    s = SimulatorStore(tmp / "people.json", v, mover=False)
    s.visits = False   # walking continuity of the wanderers (the visits come and go through the door by design)
    prev = {p["id"]: (p["x"], p["y"]) for p in s.people}
    maxstep, inside, near_dev, near_other, moved = 0.0, 0, 0, 0, {k: 0.0 for k in prev}
    s.apply({"op": "pose", "device": "hyeol", "set": {"x": 2500, "y": 6000, "heading": 0}}, True)
    h0 = s.pose("hyeol")
    t = time.monotonic()
    for i in range(30 * 60):
        if i < 60:
            s.apply({"op": "drive", "device": "hyeol", "move": 1, "turn": 0, "strafe": 0}, False)  # the devices keep working too
        s.step(1 / 30, t + i / 30)
        for p in s.people:
            st = math.hypot(p["x"] - prev[p["id"]][0], p["y"] - prev[p["id"]][1])
            if p.pop("teleported", False):   # a deliberate jump out of a no-go area / a stuck spot (2026-11-06), not walking
                st = 0.0
            maxstep, moved[p["id"]], prev[p["id"]] = max(maxstep, st), moved[p["id"]] + st, (p["x"], p["y"])
            inside += v.blocked(p["x"], p["y"], radius=p["r"], overhead=True) is not None
            near_dev += any(math.hypot(p["x"] - d["x"], p["y"] - d["y"]) < 300 + p["r"] for d in s.devices.values())
            near_other += any(o is not p and math.hypot(p["x"] - o["x"], p["y"] - o["y"]) < o["r"] + p["r"] for o in s.people)
    top = max(v.people["speed_mm_s"])
    check(len(s.people) == 3 and maxstep <= top / 30 + 0.5 and all(m > 3000 for m in moved.values()),
          "3 mock-up people walk slowly and continuously (no jumps)", f"max {maxstep * 30:.0f} mm/s (≤ {top}), walked {[round(m / 1000, 1) for m in moved.values()]} m in 60 s")
    check(inside == 0 and near_dev == 0 and near_other == 0, "people never pass through walls, 심, devices or each other",
          f"inside {inside}, through a device {near_dev}, through each other {near_other}")
    h1 = s.pose("hyeol")
    check(h1["y"] - h0["y"] > 300, "the device drives while the people walk (independent)", f"혈 Δy {h1['y'] - h0['y']:.0f} mm")
    # ToF sees a person in front, and the background again once the person is gone
    cam = MockToF(seed=3, venue=v); cam.set_pose(3000, 6000, 0, SIM_TOF_HEIGHT, 0.0)
    def centre(people):
        vals = []
        for i in range(8):
            d_, _ = cam.frame(i / 30, people); patch = d_[88:93, 118:123]; patch = patch[patch > 0]
            if patch.size >= 6:
                vals.append(float(np.median(patch)))
        return float(np.median(vals)) if vals else float("nan")
    def person(x, y, heading=180.0):
        return {"x": x, "y": y, "r": 250.0, "h": 1700.0, "parts": person_parts(x, y, heading, 250.0, 1700.0)}
    # expected: the level centre ray at 1.5 m passes the silhouette part that spans that height (here the head)
    front = min(1500 - math.sqrt(max(pt["r"] ** 2 - (SIM_TOF_HEIGHT - pt["z"]) ** 2, 0)) if pt["type"] == "sph" else 1500 - pt["r"]
                for pt in person(3000, 7500)["parts"] if (pt["type"] == "sph" and abs(SIM_TOF_HEIGHT - pt["z"]) < pt["r"]) or (pt["type"] == "cyl" and pt["z0"] <= SIM_TOF_HEIGHT <= pt["z1"] and abs(pt["x"] - 3000) < 1))
    bg = centre([]); with_p = centre([person(3000, 7500)]); gone = centre([person(5000, 7500)])
    check(abs(with_p - front) <= 40 and (not math.isfinite(bg) or bg > 3000) and (math.isnan(gone) and math.isnan(bg) or abs(gone - bg) < 40),
          "ToF: a person (silhouette) 1.5 m ahead is read at the front of the part at sensor height, the background again once the person steps aside",
          f"background {bg if not math.isfinite(bg) else round(bg)}, person {with_p:.0f} mm (expected {front:.0f}, head at 1.5 m), after {gone if not math.isfinite(gone) else round(gone)}")
    s.stop()
    check_avoidance(v, tmp)


class FullFrameToF(MockToF):
    """Reference: every person traced on every ray of the frame (no pixel window), as the ToF mock did for any person
    reaching behind the camera before 2026-10-14."""
    def _window(self, lo, hi):
        return 0, 180, 0, 240


def check_person_tof(v) -> None:
    """People in the ToF mock: only the pixels a person can cover are traced, and that gives exactly the same depth and
    confidence as tracing every person on every ray; a person wholly behind the camera costs nothing."""
    def person(x, y, heading=0.0):
        return {"x": x, "y": y, "r": 250.0, "h": 1700.0, "parts": person_parts(x, y, heading, 250.0, 1700.0)}

    X0, Y0, HD = 3000.0, 6000.0, 0.0  # open floor, facing +y (camera right = +x)
    def at(right, fwd, heading=0.0):
        return person(X0 + right, Y0 + fwd, heading)

    def pair(seed=5):
        a, b = MockToF(seed=seed, venue=v), FullFrameToF(seed=seed, venue=v)
        a.set_pose(X0, Y0, HD, SIM_TOF_HEIGHT, 0.0); b.set_pose(X0, Y0, HD, SIM_TOF_HEIGHT, 0.0)
        return a, b

    def same(people, frames=3):
        a, b = pair()
        ok = True
        for i in range(frames):
            da, ca = a.frame(i / 30, people); db, cb = b.frame(i / 30, people)
            ok &= bool(np.array_equal(da, db) and np.array_equal(ca, cb) and np.array_equal(a.last_labels, b.last_labels))
        return ok, a, da

    def box_window(cam, p):
        return cam._window((p["x"] - p["r"], 0, p["y"] - p["r"]), (p["x"] + p["r"], p["h"], p["y"] + p["r"]))

    # front, 1.5 m: seen around the centre, same as the reference
    ok, cam, d = same([at(0, 1500)])
    cols = np.nonzero((cam.last_labels == PERSON).any(axis=0))[0]
    check(ok and cols.size and cols.min() < 120 < cols.max(), "ToF people: a person 1.5 m ahead is traced only in its pixel window, same depth / confidence as tracing every ray",
          f"person columns {cols.min()}–{cols.max()}, window {box_window(cam, at(0, 1500))}")

    # field edge, 2.5 m at ±33° (half the field is 35°): seen at that edge, depth = front of the person
    for ang, side in ((33, "right"), (-33, "left")):
        r_, f_ = 2500 * math.sin(math.radians(ang)), 2500 * math.cos(math.radians(ang))
        ok, cam, d = same([at(r_, f_)])
        m = cam.last_labels == PERSON
        cols = np.nonzero(m.any(axis=0))[0]
        dep = d[m & (d > 0)]
        at_edge = cols.size and (cols.max() == 239 if side == "right" else cols.min() == 0)
        check(ok and at_edge and dep.size and 2500 - 250 - 60 <= float(np.median(dep)) <= 2500 + 60,
              f"ToF people: a person 2.5 m away at the {side} field edge ({ang:+d}°) is seen at that edge at the right distance, same as the reference",
              f"columns {cols.min() if cols.size else '-'}–{cols.max() if cols.size else '-'}, median depth {float(np.median(dep)) if dep.size else float('nan'):.0f} mm (front 2,250–2,500)")

    # beside the camera (1.5 m to the right, level with the lens): outside the 70° field, nothing traced
    a, _ = pair()
    p = at(1500, 0)
    ok, cam, _ = same([p])
    a0, _ = pair(); d0, c0 = a0.frame(0.0, []); a1, _ = pair(); d1, c1 = a1.frame(0.0, [p])
    check(ok and box_window(cam, p) is None and np.array_equal(d0, d1) and np.array_equal(c0, c1),
          "ToF people: a person right beside the camera (outside the field) is not traced, frame = no people", f"window {box_window(cam, p)}")

    # behind the camera (5.4 m, and 0.7 m behind-beside like the Windows case): skipped, frame = no people
    for right, fwd, what in ((0, -5400, "5.4 m behind"), (500, -500, "0.7 m behind-beside")):
        p = at(right, fwd)
        ok, cam, _ = same([p])
        a1, _ = pair(); d1, c1 = a1.frame(0.0, [p])
        check(ok and box_window(cam, p) is None and np.array_equal(d0, d1) and np.array_equal(c0, c1),
              f"ToF people: a person {what} the camera is skipped (window none), frame = no people", f"window {box_window(cam, p)}")

    # near the lens, the person's box crossing the camera plane: only the visible part's pixels, nothing cut off
    crossing = visible = 0
    all_same = True
    example = ""
    for fwd in range(-300, 451, 75):
        for right in range(-900, 901, 75):
            for hd in (0, 90):
                if math.hypot(right, fwd) < 300:  # the lens inside the person (cannot happen: device radius 300)
                    continue
                p = at(right, fwd, hd)
                if not (fwd - 250 < 0 < fwd + 250):
                    continue
                crossing += 1
                ok, cam, _ = same([p], frames=1)
                all_same &= ok
                if (cam.last_labels == PERSON).any():
                    visible += 1
                    if not example:
                        example = f"e.g. ({right:+d}, {fwd:+d}) → window {box_window(cam, p)}, {(cam.last_labels == PERSON).sum()} person px"
    check(all_same and visible > 0, "ToF people: persons crossing the camera plane next to the lens keep every visible pixel (same as the reference)",
          f"{crossing} placements, {visible} partly visible, all identical; {example}")

    # random people all around open-floor poses: identical to the reference
    import random
    rng = random.Random(7)
    n = diff = seen = 0
    for _ in range(6):
        while True:
            pose = (rng.uniform(1500, 9500), rng.uniform(2500, 18000), rng.uniform(0, 360))
            if v.blocked(pose[0], pose[1], radius=DEVICE_RADIUS_MM) is None:
                break
        a, b = MockToF(seed=9, venue=v), FullFrameToF(seed=9, venue=v)
        a.set_pose(*pose, SIM_TOF_HEIGHT, 0.0); b.set_pose(*pose, SIM_TOF_HEIGHT, 0.0)
        for _ in range(10):
            ppl = []
            for _ in range(rng.choice((1, 2, 3))):
                dd, ang = rng.uniform(560, 7000), math.radians(pose[2] + rng.uniform(-180, 180))
                ppl.append(person(pose[0] + dd * math.sin(ang), pose[1] + dd * math.cos(ang), rng.uniform(0, 360)))
            da, ca = a.frame(0.5, ppl); db, cb = b.frame(0.5, ppl)
            n += 1; diff += not (np.array_equal(da, db) and np.array_equal(ca, cb)); seen += bool((a.last_labels == PERSON).any())
    check(diff == 0, "ToF people: random people around 6 poses give the same depth / confidence as tracing every ray", f"{n} frames, {seen} with a person in view, {diff} different")

    # the Windows slowdown (2026-10-14 17:38): 혈 at its saved pose, people 0.7 m beside-behind and 5.4 m behind
    pose = (4005.2, 14445.8, 232.997)
    ppl = [person(8551, 5370, 30), person(4668, 14672, 200), person(8785, 16901, 120)]
    def per_frame(cls, people, n=20):
        c = cls(seed=1, venue=v); c.set_pose(*pose, SIM_TOF_HEIGHT, 0.0); c.frame(0.0, people)
        t = time.perf_counter()
        for i in range(n):
            c.frame(i / 30, people)
        return (time.perf_counter() - t) / n * 1000
    t_new, t_none, t_ref = per_frame(MockToF, ppl), per_frame(MockToF, []), per_frame(FullFrameToF, ppl, 5)
    check(t_new < t_none * 2 + 3, "ToF people: frame time with people beside / behind the camera ≈ no people (Windows 5.5 fps case)",
          f"{t_new:.1f} ms with the 3 people, {t_none:.1f} ms without, {t_ref:.1f} ms when every person is traced on every ray")


def check_tof_range(v) -> None:
    """B0410 Far mode in the venue: a surface straight ahead is read at its distance from 1 m to just under 4 m (even the
    darkest one, the black-out curtain), and beyond 4 m it is no data (black), never a wrapped near value."""
    v = load_venue("m30")   # own copy: the east middle big wall swung open (90°) so the curtain behind it is in view
    v.set_layout(v.check_layout({"big_walls": {"movable-east-2": 90}}))

    def centre_vals(pose, frames=4):
        cam = MockToF(seed=3, venue=v); cam.set_pose(*pose, SIM_TOF_HEIGHT, 0.0)
        out = []
        for i in range(frames):
            d, c = cam.frame(i / 30)
            ok = (d > 0) & (d <= 4000) & (c >= 30)
            p = d[88:93, 118:123][ok[88:93, 118:123]]
            out.append(float(np.median(p)) if p.size >= 6 else float("nan"))
        return out, cam, d, c
    for name, mk in (("black-out curtain (reflectance 0.06)", lambda dd: (11300 - dd, 10000, 90)), ("north wall (0.70)", lambda dd: (500, 19851 - dd, 0))):
        errs, missing = [], []
        for dd in range(1000, 4000, 100):
            vals, *_ = centre_vals(mk(dd))
            got = [x for x in vals if not math.isnan(x)]
            if len(got) < len(vals):
                missing.append(dd)
            errs.append(abs(float(np.median(got)) - dd) if got else math.inf)
        check(not missing and max(errs) <= 10, f"ToF Far mode: {name} straight ahead read at its distance every 100 mm from 1.0 to 3.9 m",
              f"max error {max(errs):.0f} mm, frames without a value at {missing or 'none'}")
        beyond = []
        for dd in (4100, 4500, 6000):
            vals, cam, d, c = centre_vals(mk(dd))
            beyond.append(all(math.isnan(x) for x in vals) and not d[88:93, 118:123].any())
        check(all(beyond), f"ToF Far mode: {name} beyond 4 m (4.1, 4.5, 6 m) is no data (depth 0), not a wrapped value", str(beyond))
    # a person straight ahead at 1, 2, 3 m: valid at the front of the silhouette
    def person(x, y):
        return {"x": x, "y": y, "r": 250.0, "h": 1700.0, "parts": person_parts(x, y, 180.0, 250.0, 1700.0)}
    got = {}
    for dd in (1000, 2000, 3000):
        cam = MockToF(seed=3, venue=v); cam.set_pose(3000, 6000, 0, SIM_TOF_HEIGHT, 0.0)
        for i in range(4):
            d, c = cam.frame(i / 30, [person(3000, 6000 + dd)])
        m = cam.last_labels == PERSON
        ok = (d > 0) & (d <= 4000) & (c >= 30)
        got[dd] = (round(ok[m].mean() * 100), round(float(np.median(d[m & ok]))) if (m & ok).any() else None)
    check(all(share >= 90 and med is not None and dd - 250 <= med <= dd for dd, (share, med) in got.items()),
          "ToF Far mode: a person straight ahead at 1, 2, 3 m is seen (≥ 90 % of its pixels valid, at the front of the silhouette)", str(got))
    # floor: seen only where it is within 4 m (the lower rows), never beyond
    cam = MockToF(seed=3, venue=v); cam.set_pose(3000, 6000, 0, SIM_TOF_HEIGHT, 0.0)
    for i in range(4):
        d, c = cam.frame(i / 30)
    ok = (d > 0) & (d <= 4000) & (c >= 30); fl = (cam.last_labels == 1) & ok
    rows = np.nonzero(fl.any(axis=1))[0]
    check(rows.size and rows.min() > 120 and d[fl].max() <= 4000 and d[fl].min() >= 2000,
          "ToF Far mode: the floor is seen in the lower rows up to 4 m (level camera at 1.0 m since 2026-11-06: nearest floor in view ≈ 2.1 m)",
          f"rows {rows.min()}–{rows.max()}, depth {d[fl].min():.0f}–{d[fl].max():.0f} mm" if rows.size else "none")


def venue_inside(poly, x, y):
    from venue import _inside
    return bool(_inside(poly, x, y))


def check_avoidance(v, tmp: Path) -> None:
    """People step out of a driving device's way; a device stops at a person only when the person has nowhere to go."""
    def scene(name, person_xy, person_heading, device, dpose, drive, seconds, person_target=None):
        s = SimulatorStore(tmp / f"av-{name}.json", v, mover=False)
        s.visits = False   # these scenes test dodging, not the come-look-leave visits
        p = s.people[0]
        for other in s.people[1:]:          # park the other two well away (they keep their own areas)
            other.update(wait=1e9)
        p.update(x=float(person_xy[0]), y=float(person_xy[1]), heading=float(person_heading), v=0.0, wait=1e9 if person_target is None else 0.0,
                 tx=None if person_target is None else float(person_target[0]), ty=None if person_target is None else float(person_target[1]))
        s.apply({"op": "pose", "device": device, "set": dict(zip(("x", "y", "heading"), dpose))}, True)
        stops, min_gap, inside, maxstep, prev = 0, math.inf, 0, 0.0, (p["x"], p["y"])
        start = s.pose(device)
        t = time.monotonic()
        for i in range(round(seconds * 30)):
            s.apply(dict({"op": "drive", "device": device}, **drive), False)
            s.step(1 / 30, time.monotonic())  # inputs are stamped with the real clock (0.6 s repeat timeout)
            m = s.motion[device]
            stops += m["blocked"] == p["id"]
            d = s.pose(device)
            min_gap = min(min_gap, math.hypot(p["x"] - d["x"], p["y"] - d["y"]) - DEVICE_RADIUS_MM - p["r"])
            inside += v.blocked(p["x"], p["y"], radius=p["r"], overhead=True) is not None
            maxstep = max(maxstep, math.hypot(p["x"] - prev[0], p["y"] - prev[1])); prev = (p["x"], p["y"])
        end = s.pose(device)
        s.stop()
        return {"stops": stops, "gap": min_gap, "inside": inside, "speed": maxstep * 30, "moved": math.hypot(end["x"] - start["x"], end["y"] - start["y"]),
                "cornered": p["cornered"], "person": (round(p["x"]), round(p["y"]))}
    top = max(v.people["speed_mm_s"]) + 1
    r = scene("still", (3000, 7000), 90, "maek", (3000, 4000, 0), {"move": 1, "turn": 0, "strafe": 0}, 7.0)
    check(r["stops"] == 0 and r["gap"] >= 0 and r["inside"] == 0 and r["speed"] <= top and r["moved"] > 2800,
          "a standing person in the device's path steps aside early; the device keeps going (no stop, no overlap)",
          f"맥 moved {r['moved']:.0f} mm, person stops {r['stops']}, closest gap {r['gap']:.0f} mm, person now {r['person']}, person max {r['speed']:.0f} mm/s")
    r = scene("towards", (3000, 7800), 180, "hyeol", (3000, 4000, 0), {"move": 1, "turn": 0, "strafe": 0}, 7.0, person_target=(3000, 3000))
    check(r["stops"] == 0 and r["gap"] >= 0 and r["inside"] == 0 and r["speed"] <= top and r["moved"] > 2800,
          "a person walking toward a driving device turns away before contact; the device keeps going",
          f"혈 moved {r['moved']:.0f} mm, stops {r['stops']}, closest gap {r['gap']:.0f} mm, person max {r['speed']:.0f} mm/s")
    r = scene("wall", (6500, 19400), 0, "maek", (4200, 18900, 90), {"move": 1, "turn": 0, "strafe": 0}, 8.0)
    check(r["stops"] == 0 and r["gap"] >= 0 and r["inside"] == 0 and r["moved"] > 3000,
          "a person standing near the north wall steps away from the wall out of the path; the device keeps going",
          f"맥 moved {r['moved']:.0f} mm, stops {r['stops']}, closest gap {r['gap']:.0f} mm, person now {r['person']}, inside walls {r['inside']}")
    # the corner: south wall × the closed east-south big wall and the south-east column's mast (its hinge end) — the
    # north-east corner became a closed pocket (big wall foot, mast, north wall; 2026-11-06), people are kept out of it
    r = scene("cornered", (10140, 580), 135, "hyeol", (8900, 1850, 135), {"move": 1, "turn": 0, "strafe": 0}, 6.0)
    check(r["stops"] > 0 and r["gap"] >= -1 and r["inside"] == 0,
          "only a person with no free space (south wall × closed east-south big wall) stops the device — reported as blocked; "
          "after PEOPLE_MOTION stuck_s the person slips out (2026-11-06), so the device is not stuck for good",
          f"stops {r['stops']}, cornered at the end {r['cornered']}, closest gap {r['gap']:.0f} mm, person now {r['person']}")


def check_attach(v, tmp: Path) -> None:
    s = SimulatorStore(tmp / "att.json", v, mover=False, people=False)
    check(s.hyeol_state()["attached"] and s.hyeol_state()["state"] == "in_use", "혈 starts attached (사용 중)", str(s.hyeol_state()["label"]))
    snap = s.apply({"op": "attach", "device": "hyeol", "attached": False}, True)
    saved = json.loads((tmp / "att.json").read_text(encoding="utf-8"))
    check(snap["hyeol"]["state"] == "idle" and not snap["hyeol"]["collecting"] and saved["hyeol_attached"] is False,
          "detach → 아이들링, collection off, saved at once", f"{snap['hyeol']['label']}, file hyeol_attached={saved['hyeol_attached']}")
    s2 = SimulatorStore(tmp / "att.json", v, mover=False, people=False)
    check(s2.hyeol_state()["attached"] is False, "restart keeps 혈 detached (아이들링)")
    run(s2, "hyeol", 1, 0, 1.0)
    check(s2.motion["hyeol"]["v"] > 0, "detached 혈 can still be moved (only sensor collection stops)", f"v {s2.motion['hyeol']['v']:.0f}")
    coast(s2, "hyeol", 1.0)
    for bad in ({"device": "maek", "attached": False}, {"device": "hyeol", "attached": 0}, {"device": "hyeol"}):
        try:
            s2.apply(dict({"op": "attach"}, **bad), True)
            ok = False
        except ValueError:
            ok = True
        check(ok, f"invalid attach rejected {bad}")
    s2.apply({"op": "attach", "device": "hyeol", "attached": True}, True)
    check(SimulatorStore(tmp / "att.json", v, mover=False, people=False).hyeol_state()["attached"], "re-attach → 사용 중, saved")


def check_up_changes(tmp: Path) -> None:
    """The up distance follows the 혈 pose through the geometry. M30's only documented ceiling is flat (4.2 m), so a
    test-only fixture with a lower ceiling zone is used here; it is not a venue."""
    from venue import Venue
    fixture = {"format": "ripples-venue/1", "id": "fixture", "name": "test fixture (not a venue)", "floor": {"z": 0}, "ceiling": {"z": 4200},
               "zones": [{"kind": "ceiling", "rect": [0, 5000, 6000, 10000], "z": 3000}],
               "solids": [{"id": "box", "kind": "wall", "rect": [2500, 2700, 3500, 3300]}],
               "walkable": [[0, 0], [6000, 0], [6000, 10000], [0, 10000]],
               "start": {"hyeol": {"x": 1000, "y": 3000, "heading": 0}, "maek": {"x": 5000, "y": 1000, "heading": 0}}}
    fv = Venue(fixture)
    s = SimulatorStore(tmp / "fx.json", fv, mover=False)
    ups, centres = [], []
    for y in (3000, 4500, 6000):
        s.apply({"op": "pose", "device": "hyeol", "set": {"x": 1000, "y": y, "heading": 90}}, True)
        ups.append(s.up_distance())
        centres.append(centre_mm(fv, 1000, y, 90)[0])
    check(ups == [2200.0, 2200.0, 1000.0], "fixture: up distance changes when 혈 moves under a lower ceiling (4.2 m → 3.0 m)", str(ups))
    check(np.isfinite(centres[0]) and abs(centres[0] - 1500) <= 40 and not np.isfinite(centres[1]), "fixture: ToF centre changes with the pose (box 1.5 m ahead → nothing within 4 m)",
          str([round(c) if np.isfinite(c) else None for c in centres]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="m30")
    args = ap.parse_args()
    v = load_venue(args.venue)
    if args.venue == "m30":
        check_m30_geometry(v)
        check_m30_boundary(v)
        check_m30_sensors(v)
        check_presets()
    with tempfile.TemporaryDirectory() as d:
        if args.venue == "m30":
            check_motion(v, Path(d))
            check_attach(v, Path(d))
            check_slide(v, Path(d))
            check_up_changes(Path(d))
            ups = {(x, y): v.up_distance(x, y, 2000) for x, y in ((1000, 1000), (3000, 13000), (10000, 19000))}
            check(set(ups.values()) == {2200.0}, "M30: up distance 2.2 m wherever nothing hangs above (only the flat official 4.2 m ceiling is documented)", str(ups))
            check_mockup(v, Path(d))
            check_person_tof(v)
            check_tof_range(v)
    width = max(len(n) for _, n, _ in results) + 2
    for ok, name, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}{detail}")
    failed = sum(not ok for ok, _, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} passed ({args.venue})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
