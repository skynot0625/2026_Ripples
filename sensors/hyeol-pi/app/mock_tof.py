"""Sensor-like mock depth/confidence frames for --mock (240×180, about 4 m range).

This is a physical approximation, not a recording: there are no real Arducam B0410 captures in this project, so
values (field of view, amplitude scale, noise) are assumptions chosen to behave like a low-resolution ToF sensor:

- A fixed camera 1.2 m above the floor, tilted 8° down, in a small room: floor, ceiling, side walls, a back wall at
  3.6 m with a black poster, a doorway on the right that opens into a corridor ending at 7 m (beyond range), a dark box,
  a person walking slowly and a small object swinging fast (for the temporal filter / latency check in the README).
- Amplitude (returned as confidence) ∝ reflectance × cos(incidence) / distance², so far, grazing and dark surfaces
  come back weak and are dropped by the confidence threshold.
- Every pixel mixes 2×2 sub-rays as phasors (like a ToF pixel integrating light from both sides of an edge): edges get
  intermediate "flying" depths and, where the two sides cancel, low amplitude and gaps.
- Depth noise grows as amplitude falls (so with distance); noise is correlated over time (AR(1)), so a static wall
  wobbles slightly instead of re-randomising every frame, and dropouts flicker only where the signal is marginal.
- Returns beyond the 4 m range are reported as invalid (depth 0, very low confidence), never as a wrapped depth.

Same seed + same sequence of t → same frames (see mock_check.py).

With a venue (venue.py, --venue m30) the camera stands in that venue's geometry instead: the same sensor model
(sub-ray mixing, amplitude, noise, invalid rules) is applied to rays traced against the venue's floor, ceiling and
solids. The test room's moving person / swinging object are left out; the people passed to frame() (the simulator's
mock-up people, one shared state) are traced as their simple silhouettes (simulator_store.person_parts: upright
cylinders for legs / torso / arms and a sphere for the head) at their current positions.
"""

from __future__ import annotations

import math

import numpy as np

W, H = 240, 180
MAX_RANGE_MM = 4000.0
SS = 2                       # sub-rays per pixel side (2×2)
HFOV_DEG = 70.0              # assumed horizontal field of view
PITCH_DEG = -8.0             # camera tilt (negative = looking down)
CAM_Y = 1200.0               # camera height above the floor, mm
AMP_K = 1800.0               # amplitude scale: a 0.7-reflectance wall at 3.6 m ≈ 90 (procedural test room)
# Venue mode (혈 in M30) models the B0410 Far mode (4 m): the amplitude scale is set so that the darkest venue surface
# (black-out curtain, reflectance 0.06 assumed in venue.KINDS) facing the camera at 4.0 m still reaches confidence ≈ 34
# (> the server's default threshold 30); every brighter surface, people and the floor in the lower rows are then seen
# up to 4 m, and beyond 4 m / no hit stays invalid (phase / far rule below). With AMP_K the curtain dropped out at
# about 1.9 m and the floor never reached the threshold (2026-10-14). Not checked against a real B0410 capture.
AMP_K_FAR = 9000.0
NOISE_AR = 0.8               # temporal correlation of the per-pixel noise
NEAR_MM = 1.0                # moving objects are clipped this far in front of the camera (_window)

# surface ids (also used by mock_check.py)
NONE, FLOOR, CEILING, BACK, POSTER, LEFT, RIGHT, CORRIDOR, BOX, PERSON, OBJECT, FRONT = range(12)
STATIC_IDS = (FLOOR, CEILING, BACK, POSTER, LEFT, RIGHT, CORRIDOR, BOX, FRONT)

# The simplified test room (mm, floor plane X right / Z forward, ceiling 2700). Not a real venue. The simulator
# (simulator_store.py, simulator.html) uses the same outline for its map and to keep the devices inside the room.
ROOM = {
    "room": {"x0": -1800, "x1": 1800, "z0": -1200, "z1": 3600},     # main room (front wall Z=-1200, back wall Z=3600)
    "corridor": {"x0": 800, "x1": 1800, "z0": 3600, "z1": 7000},    # doorway on the right, ends beyond the 4 m range
    "box": {"x0": -1300, "x1": -700, "z0": 1500, "z1": 2100},       # dark box, 750 mm high
    "poster": {"x0": -1400, "x1": -600, "z": 3600},                 # black poster on the back wall
    "ceiling": 2700,
}
# Simulator mounting on the 혈 device (fixed, not adjustable): ToF facing forward, distance sensor straight up.
SIM_TOF_HEIGHT = 1000.0   # 2026-11-06 (user): at the bottom of 혈's rear A-frame (the underside of the top), was 1,500
SIM_TOF_BACK_MM = 600.0   # how far behind the device centre the ToF sits; it looks backward (heading + 180°)
SIM_TOF_PITCH = 0.0
SIM_DISTANCE_HEIGHT = 2000.0


def vertical_distance_mm(x: float, z: float, height: float = SIM_DISTANCE_HEIGHT) -> float:
    """Straight-up distance from a sensor at (x, height, z) to the test room's flat ceiling."""
    return ROOM["ceiling"] - height


def _rays(pitch_deg: float = PITCH_DEG, heading_deg: float = 0.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    fx = (W / 2) / math.tan(math.radians(HFOV_DEG / 2))
    u = (np.arange(W * SS) + 0.5) / SS - 0.5
    v = (np.arange(H * SS) + 0.5) / SS - 0.5
    uu, vv = np.meshgrid(u, v)
    x, y, z = (uu - (W - 1) / 2) / fx, -(vv - (H - 1) / 2) / fx, np.ones_like(uu)
    p = math.radians(pitch_deg)
    # rotate about X: forward (0,0,1) becomes (0, sin p, cos p), so a negative pitch looks down
    y, z = y * math.cos(p) + z * math.sin(p), -y * math.sin(p) + z * math.cos(p)
    if heading_deg:
        # then about Y: heading 90° faces +X (turning right seen from above)
        h = math.radians(heading_deg)
        x, z = x * math.cos(h) + z * math.sin(h), -x * math.sin(h) + z * math.cos(h)
    n = np.sqrt(x * x + y * y + z * z)
    return x / n, y / n, z / n


def _plane(d: np.ndarray, o: float, target: float) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (target - o) / d
    return np.where(t > 1e-6, t, np.inf)


def _grid() -> tuple[np.ndarray, np.ndarray]:
    """Pinhole directions of the sub-ray columns (x, right) and rows (y, up) for forward = 1 (venue.trace)."""
    fx = (W / 2) / math.tan(math.radians(HFOV_DEG / 2))
    u = (np.arange(W * SS) + 0.5) / SS - 0.5
    v = (np.arange(H * SS) + 0.5) / SS - 0.5
    return (u - (W - 1) / 2) / fx, -(v - (H - 1) / 2) / fx


class MockToF:
    def __init__(self, seed: int | None = None, venue=None) -> None:
        self.rng = np.random.default_rng(seed)
        self.venue = venue  # venue.Venue or None (procedural test room)
        self.amp_k = AMP_K if venue is None else AMP_K_FAR
        if venue is None:
            # default view: fixed camera at (0, 1.2 m, 0) looking forward, 8° down (the original procedural mock)
            self.pose = (0.0, 0.0, 0.0, CAM_Y, PITCH_DEG)  # x, z, heading°, height, pitch°
            self.dx, self.dy, self.dz = _rays()
        else:
            start = venue.start["hyeol"]
            self.pose = (start["x"], start["y"], start["heading"] % 360.0, SIM_TOF_HEIGHT, SIM_TOF_PITCH)
            self.xc, self.yr = _grid()
            self.dx, self.dy, self.dz = _rays(0.0, self.pose[2])  # per-pixel rays, for the moving people
        self._static()
        self.noise = np.zeros((H, W), np.float32)
        self.cnoise = np.zeros((H, W), np.float32)
        self.last_labels = np.zeros((H, W), np.int8)

    def set_pose(self, x: float, z: float, heading_deg: float, height: float = SIM_TOF_HEIGHT, pitch_deg: float = SIM_TOF_PITCH) -> None:
        """Move the camera in the test room (simulator). Re-traces the static room only when the pose changes."""
        pose = (float(x), float(z), float(heading_deg) % 360.0, float(height), float(pitch_deg))
        lv = getattr(self.venue, "layout_version", 0)   # the exhibition layout (venue.py) changed: re-trace in place
        if pose == self.pose and lv == getattr(self, "_layout_version", None):
            return
        self._layout_version = lv
        self.pose = pose
        if pose[4] != 0 and self.venue is not None:
            raise ValueError("venue mode traces a level camera only (SIM_TOF_PITCH = 0)")
        self.dx, self.dy, self.dz = _rays(pose[4], pose[2])
        self._static()

    @property
    def origin(self) -> tuple[float, float, float]:
        return self.pose[0], self.pose[3], self.pose[1]

    # --- static room (computed once) ------------------------------------------------------------------------------
    def _static(self) -> None:
        if self.venue is not None:
            x, y, heading, height = self.pose[0], self.pose[1], self.pose[2], self.pose[3]
            t, lab, alb, cos = self.venue.trace((x, y, self.venue.floor_at(x, y) + height), heading, self.xc, self.yr)
            self.s_t, self.s_lab, self.s_alb, self.s_cos = t, lab, alb, cos
            sums = self._sums(t, lab, alb, cos)
            self.p_sums, self.p_lab, self.p_mixed = sums[:5], sums[5], sums[6]
            return
        dx, dy, dz = self.dx, self.dy, self.dz
        ox, oy, oz = self.origin
        t = np.full(dx.shape, np.inf); lab = np.zeros(dx.shape, np.int8); alb = np.zeros(dx.shape); cos = np.zeros(dx.shape)

        def take(tc, label, albedo, cosv):
            nonlocal t
            m = tc < t
            t = np.where(m, tc, t); lab[m] = label; alb[m] = albedo if np.isscalar(albedo) else albedo[m]; cos[m] = np.abs(cosv[m])

        # floor / ceiling (unbounded)
        take(_plane(dy, oy, 0.0), FLOOR, 0.3, dy)
        take(_plane(dy, oy, 2700.0), CEILING, 0.75, dy)
        # back wall Z=3600 for X ≤ 800 (with a black poster), corridor end wall Z=7000 for 800 < X ≤ 1800
        tb = _plane(dz, oz, 3600.0); xb, yb = ox + dx * tb, oy + dy * tb
        back = np.where((xb >= -1800) & (xb <= 800), tb, np.inf)
        poster = (xb >= -1400) & (xb <= -600) & (yb >= 1100) & (yb <= 1900)
        take(back, BACK, np.where(poster, 0.04, 0.7), dz)
        lab[(lab == BACK) & poster] = POSTER
        te = _plane(dz, oz, 7000.0); xe = ox + dx * te
        take(np.where((xe > 800) & (xe <= 1800), te, np.inf), CORRIDOR, 0.7, dz)
        # side walls; the doorway edge wall X=800 runs along the corridor
        tl = _plane(dx, ox, -1800.0); zl = oz + dz * tl; take(np.where((zl >= -1200) & (zl <= 3600), tl, np.inf), LEFT, 0.7, dx)
        tr = _plane(dx, ox, 1800.0); zr = oz + dz * tr; take(np.where((zr >= -1200) & (zr <= 7000), tr, np.inf), RIGHT, 0.7, dx)
        tc = _plane(dx, ox, 800.0); zc = oz + dz * tc; take(np.where((zc >= 3600) & (zc <= 7000), tc, np.inf), CORRIDOR, 0.7, dx)
        # front wall Z=-1200 closes the room behind the default view (only seen when the simulator turns the camera)
        tf = _plane(dz, oz, -1200.0); xf = ox + dx * tf; take(np.where((xf >= -1800) & (xf <= 1800), tf, np.inf), FRONT, 0.7, dz)
        # dark box (low reflectance) on the floor, front-left
        tb_, nb = self._box(dx, dy, dz, (ox, oy, oz), (-1300, 0, 1500), (-700, 750, 2100))
        take(tb_, BOX, 0.12, nb)
        self.s_t, self.s_lab, self.s_alb, self.s_cos = t, lab, alb, cos
        sums = self._sums(t, lab, alb, cos)
        self.p_sums, self.p_lab, self.p_mixed = sums[:5], sums[5], sums[6]

    @staticmethod
    def _box(dx, dy, dz, o, lo, hi):
        with np.errstate(divide="ignore", invalid="ignore"):
            ts = [((lo[i] - o[i]) / d, (hi[i] - o[i]) / d) for i, d in enumerate((dx, dy, dz))]
        tmin = np.maximum.reduce([np.minimum(a, b) for a, b in ts])
        tmax = np.minimum.reduce([np.maximum(a, b) for a, b in ts])
        hit = (tmax >= tmin) & (tmin > 1e-6)
        # the face hit is the axis whose entry time equals tmin
        axis = np.argmax(np.stack([np.minimum(a, b) for a, b in ts]), axis=0)
        cosv = np.choose(axis, (dx, dy, dz))
        return np.where(hit, tmin, np.inf), cosv

    # --- dynamic objects (on a block of rays) --------------------------------------------------------------------
    @staticmethod
    def _sphere(dx, dy, dz, o, c, r):
        ox, oy, oz = o[0] - c[0], o[1] - c[1], o[2] - c[2]
        b = dx * ox + dy * oy + dz * oz
        disc = b * b - (ox * ox + oy * oy + oz * oz - r * r)
        t = -b - np.sqrt(np.maximum(disc, 0))
        t = np.where((disc > 0) & (t > 1e-6), t, np.inf)
        nx, ny, nz = (dx * t + ox) / r, (dy * t + oy) / r, (dz * t + oz) / r
        return t, np.nan_to_num(dx * nx + dy * ny + dz * nz)

    @staticmethod
    def _cylinder(dx, dy, dz, o, px, pz, r, y0, y1):
        ox, oz = o[0] - px, o[2] - pz
        a = dx * dx + dz * dz
        b = 2 * (dx * ox + dz * oz)
        c = ox * ox + oz * oz - r * r
        disc = b * b - 4 * a * c
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (-b - np.sqrt(np.maximum(disc, 0))) / (2 * a)
        y = o[1] + dy * t
        t = np.where((disc > 0) & (t > 1e-6) & (y >= y0) & (y <= y1), t, np.inf)
        nx, nz = (dx * t + ox) / r, (dz * t + oz) / r
        return t, np.nan_to_num(dx * nx + dz * nz)

    def _sums(self, tt, lab, alb, cos):
        """Per-pixel phasor sums of a block of sub-rays (shape multiple of SS)."""
        h, w = tt.shape[0] // SS, tt.shape[1] // SS
        fin = np.isfinite(tt)
        r = np.where(fin, tt, 0.0)
        amp = np.where(fin, self.amp_k * alb * cos / np.maximum(r / 1000.0, 0.05) ** 2, 0.0)
        phase = 2 * np.pi * r / MAX_RANGE_MM
        block = lambda a: a.reshape(h, SS, w, SS).sum(axis=(1, 3))
        lb = lab.reshape(h, SS, w, SS)
        return (block(amp * np.cos(phase)), block(amp * np.sin(phase)), block(amp), block(amp * (fin & (tt > MAX_RANGE_MM))),
                block(amp * r), lb[:, 0, :, 0].copy(), lb.min(axis=(1, 3)) != lb.max(axis=(1, 3)))

    def _window(self, lo, hi):
        """Pixel window (r0, r1, c0, c1) covering the part of the world-space box lo–hi in front of the camera, or None
        when none of it is. The box is first clipped at a plane NEAR_MM in front of the camera (corners behind it are
        replaced by the points where the box edges cross it), so a box reaching behind the camera only covers the
        pixels of its visible part (a part right beside the lens projects far out and is clamped to the frame edge, so
        nothing visible is cut) and a box wholly behind the camera is skipped. Hits closer than NEAR_MM cannot occur."""
        fx = (W / 2) / math.tan(math.radians(HFOV_DEG / 2))
        cx_, cy_, cz_ = self.origin
        h, p = math.radians(self.pose[2]), math.radians(self.pose[4])
        cam = []  # camera space (x right, y up, z forward); corner index = 4·ix + 2·iy + iz
        for X in (lo[0], hi[0]):
            for Y in (lo[1], hi[1]):
                for Z in (lo[2], hi[2]):
                    xw, yw, zw = X - cx_, Y - cy_, Z - cz_
                    xw, zw = xw * math.cos(h) - zw * math.sin(h), xw * math.sin(h) + zw * math.cos(h)  # undo heading
                    yc, zc = yw * math.cos(p) - zw * math.sin(p), yw * math.sin(p) + zw * math.cos(p)  # undo pitch
                    cam.append((xw, yc, zc))
        pts = [c for c in cam if c[2] >= NEAR_MM]
        if not pts:
            return None
        if len(pts) < 8:
            for i in range(8):
                for bit in (1, 2, 4):
                    j = i | bit
                    if j == i:
                        continue
                    (x0, y0, z0), (x1, y1, z1) = cam[i], cam[j]
                    if (z0 - NEAR_MM) * (z1 - NEAR_MM) < 0:
                        k = (NEAR_MM - z0) / (z1 - z0)
                        pts.append((x0 + k * (x1 - x0), y0 + k * (y1 - y0), NEAR_MM))
        us = [x / z * fx + (W - 1) / 2 for x, _, z in pts]
        vs = [-y / z * fx + (H - 1) / 2 for _, y, z in pts]
        c0, c1 = max(0, int(math.floor(min(us))) - 2), min(W, int(math.ceil(max(us))) + 3)
        r0, r1 = max(0, int(math.floor(min(vs))) - 2), min(H, int(math.ceil(max(vs))) + 3)
        return (r0, r1, c0, c1) if r0 < r1 and c0 < c1 else None

    # --- frame ----------------------------------------------------------------------------------------------------------
    def frame(self, t: float, people=None) -> tuple[np.ndarray, np.ndarray]:
        """people (venue mode): [{x, y, r, h, parts}, …] in venue plan mm (simulator_store.people_for_tof)."""
        # a person walking across and toward/away from the camera, up to about 0.5 m/s (continuous in t)
        px, pz = 900 * math.sin(0.55 * t), 2300 + 600 * math.sin(0.35 * t + 1.0)
        # a small object swinging fast (crosses the view in about 0.5 s) for the temporal filter / latency check
        ph = (t / 0.5) % 2
        bx = -900 + 1800 * (ph if ph < 1 else 2 - ph)

        # The room never changes: start from its precomputed per-pixel sums and redo only the pixels the moving
        # objects can cover (every object is traced in every window, so overlapping windows agree).
        re, im, a_sum, far_a, ar = (x.copy() for x in self.p_sums)
        lab_p, mixed_p = self.p_lab.copy(), self.p_mixed.copy()
        if self.venue is not None:
            floor = self.venue.floor_at(self.pose[0], self.pose[1])
            # camera origin is (x, height above floor, y): put people in the same frame (z up relative to that floor)
            people = [p for p in (people or []) if math.hypot(p["x"] - self.pose[0], p["y"] - self.pose[1]) < 9000]
            windows = [self._window((p["x"] - p["r"], 0, p["y"] - p["r"]), (p["x"] + p["r"], p["h"], p["y"] + p["r"])) for p in people]
        else:
            windows = [self._window((px - 220, 0, pz - 220), (px + 220, 1710, pz + 220)),
                       self._window((bx - 70, 930, 1030), (bx + 70, 1070, 1170))]
        for win in windows:
            if win is None:
                continue
            r0, r1, c0, c1 = win
            rs, cs = slice(r0 * SS, r1 * SS), slice(c0 * SS, c1 * SS)
            dx, dy, dz = self.dx[rs, cs], self.dy[rs, cs], self.dz[rs, cs]
            tt, lab, alb, cos = self.s_t[rs, cs].copy(), self.s_lab[rs, cs].copy(), self.s_alb[rs, cs].copy(), self.s_cos[rs, cs].copy()

            def take(tc, label, albedo, cosv, sub=(slice(None), slice(None))):
                t_, m = tt[sub], tc < tt[sub]
                t_[m] = tc[m]; lab[sub][m] = label; alb[sub][m] = albedo; cos[sub][m] = np.abs(cosv[m])

            o = self.origin
            if self.venue is not None:
                for p in people:  # mock-up people (clothes / skin ~0.4 reflectance, assumed): the shared silhouette parts
                    for part in p["parts"]:
                        # each part only on the rays of its own pixel window inside this one (no other ray can hit it)
                        if part["type"] == "cyl":
                            box = (part["x"] - part["r"], part["z0"], part["y"] - part["r"]), (part["x"] + part["r"], part["z1"], part["y"] + part["r"])
                        else:
                            box = (part["x"] - part["r"], part["z"] - part["r"], part["y"] - part["r"]), (part["x"] + part["r"], part["z"] + part["r"], part["y"] + part["r"])
                        pw = self._window(*box)
                        if pw is None:
                            continue
                        a0, a1, b0, b1 = max(pw[0], r0), min(pw[1], r1), max(pw[2], c0), min(pw[3], c1)
                        if a0 >= a1 or b0 >= b1:
                            continue
                        sub = (slice((a0 - r0) * SS, (a1 - r0) * SS), slice((b0 - c0) * SS, (b1 - c0) * SS))
                        sx, sy, sz = dx[sub], dy[sub], dz[sub]
                        if part["type"] == "cyl":
                            tc, cc = self._cylinder(sx, sy, sz, o, part["x"], part["y"], part["r"], part["z0"], part["z1"])
                        else:
                            tc, cc = self._sphere(sx, sy, sz, o, (part["x"], part["z"], part["y"]), part["r"])
                        take(tc, PERSON, 0.4, cc, sub)
            else:
                tc, cc = self._cylinder(dx, dy, dz, o, px, pz, 220, 0, 1450); take(tc, PERSON, 0.4, cc)
                ts, cs_ = self._sphere(dx, dy, dz, o, (px, 1600, pz), 110); take(ts, PERSON, 0.12, cs_)  # dark hair: weak return
                tb, cb = self._sphere(dx, dy, dz, o, (bx, 1000, 1100), 70); take(tb, OBJECT, 0.3, cb)
            w_re, w_im, w_a, w_far, w_ar, w_lab, w_mixed = self._sums(tt, lab, alb, cos)
            re[r0:r1, c0:c1], im[r0:r1, c0:c1], a_sum[r0:r1, c0:c1], far_a[r0:r1, c0:c1], ar[r0:r1, c0:c1] = w_re, w_im, w_a, w_far, w_ar
            lab_p[r0:r1, c0:c1], mixed_p[r0:r1, c0:c1] = w_lab, w_mixed

        far_share = far_a / np.maximum(a_sum, 1e-9)
        mean_r = ar / np.maximum(a_sum, 1e-9)
        pix_amp = np.hypot(re, im) / (SS * SS)
        depth = (np.arctan2(im, re) % (2 * np.pi)) / (2 * np.pi) * MAX_RANGE_MM

        # temporally correlated noise: depth jitter ∝ 1/amplitude (+ a little with distance), confidence ripple
        k = math.sqrt(1 - NOISE_AR ** 2)
        self.noise = (NOISE_AR * self.noise + k * self.rng.standard_normal((H, W))).astype(np.float32)
        self.cnoise = (NOISE_AR * self.cnoise + k * self.rng.standard_normal((H, W))).astype(np.float32)
        sigma = 700.0 / np.maximum(pix_amp, 1.0) + 0.0015 * depth
        depth = depth + sigma * self.noise
        conf = np.clip(pix_amp * (1 + 0.08 * self.cnoise), 0, 255)

        # invalid: no usable return, mostly beyond the 4 m range, or a phase that wrapped around near 4 m
        # (a straddling pixel would otherwise read close to 0 mm) — never a wrapped depth
        wrapped = np.abs(depth - mean_r) > MAX_RANGE_MM / 2
        out = (far_share > 0.5) | wrapped
        conf = np.where(out, np.minimum(conf, 6.0), conf)
        invalid = out | (conf < 4.0)
        depth = np.where(invalid, 0.0, np.clip(depth, 1.0, MAX_RANGE_MM))

        # dominant surface per pixel and edge (mixed sub-ray) pixels, for tests
        self.last_labels, self.last_mixed = lab_p, mixed_p
        return depth.astype(np.float32), conf.astype(np.float32)
