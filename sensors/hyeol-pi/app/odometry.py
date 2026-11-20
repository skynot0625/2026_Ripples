"""혈 움직임 추정 (2026-11-10): where the ToF camera has moved / turned, guessed from its own depth frames.

User: "필요했던 건 윤곽선을 토대로 어느 정도 움직임 추측하는 것 정도", "어긋나는 건 괜찮아". 혈 moves on the floor,
so only three numbers are estimated: x, y (mm, venue plane) and heading (°, the same convention as the simulator's
tof_pose: forward = (sin h, cos h), right = (cos h, −sin h)).

Each frame: the depth samples at wall / person height (not floor, not ceiling) become points seen from above (camera
X right, Z forward, metres) — like one LiDAR scan. That scan is matched to a key scan with 2D ICP (nearest neighbours,
trimmed, Procrustes); the result composed with the key scan's pose is the camera's pose now. A new key scan is taken
after a clear move or turn, so standing still does not drift. A poor match (few points, a large error) keeps the last
pose. Errors add up over time (no loop closure) — accepted.

The camera model is the one the browser uses for 공간선 (mock_tof: 240×180, 70° wide, radial distance, level, 1.0 m
above the floor); the real B0410's angle / depth convention is still unconfirmed (see PROGRESS).
"""

from __future__ import annotations

import math
import threading
import time

import numpy as np

HFOV_DEG = 70.0
CAM_HEIGHT_M = 1.0
BAND_M = (0.25, 2.3)        # heights kept: above the floor, below a 2.4 m+ ceiling / beams (M30 beams 2.9 m)
RANGE_M = (0.25, 4.0)       # forward distance kept (B0410 Far mode: 4 m)
VOXEL_M = 0.05
MAX_POINTS = 500
MIN_POINTS = 40
KEY_MOVE_M, KEY_TURN_DEG = 0.25, 12.0
MAX_RMSE_M, MIN_INLIERS = 0.12, 0.35


def scan_points(depth: np.ndarray, valid: np.ndarray, step: int = 2) -> np.ndarray:
    """Depth (H×W mm, radial) + valid mask → (N, 2) points (X right, Z forward, m) at wall / person height."""
    h, w = depth.shape
    fx = (w / 2) / math.tan(math.radians(HFOV_DEG / 2))
    d = depth[::step, ::step].astype(np.float32)
    ok = valid[::step, ::step] & (d > 0)
    xn = ((np.arange(0, w, step) - (w - 1) / 2) / fx)[None, :]
    yn = (-(np.arange(0, h, step) - (h - 1) / 2) / fx)[:, None]
    z = d / 1000 / np.sqrt(1 + xn * xn + yn * yn)
    x, height = xn * z, CAM_HEIGHT_M + yn * z
    keep = ok & (height > BAND_M[0]) & (height < BAND_M[1]) & (z > RANGE_M[0]) & (z < RANGE_M[1])
    pts = np.stack([x[keep], z[keep]], axis=1)
    if not len(pts):
        return pts
    _, idx = np.unique(np.floor(pts / VOXEL_M).astype(np.int32), axis=0, return_index=True)   # one point per 5 cm cell
    pts = pts[np.sort(idx)]
    if len(pts) > MAX_POINTS:
        pts = pts[np.linspace(0, len(pts) - 1, MAX_POINTS).astype(int)]
    return pts


def _rot(th: float) -> np.ndarray:
    c, s = math.cos(th), math.sin(th)
    return np.array([[c, -s], [s, c]])


def icp(src: np.ndarray, dst: np.ndarray, init=(0.0, 0.0, 0.0), iters: int = 25) -> tuple[float, float, float, float, float]:
    """Find th, t with R(th)·src + t ≈ dst. → (th rad, tx, tz m, rmse m of the kept pairs, share of pairs kept)."""
    th, t = init[0], np.array(init[1:], dtype=np.float64)
    rmse, share = math.inf, 0.0
    for _ in range(iters):
        moved = src @ _rot(th).T + t
        d2 = ((moved[:, None, :] - dst[None, :, :]) ** 2).sum(-1)
        j = d2.argmin(1)
        dist = np.sqrt(d2[np.arange(len(src)), j])
        cut = max(0.08, float(np.percentile(dist, 75)) * 1.5)   # trim: no pairs across a gap (new / lost parts of the view)
        keep = dist < cut
        if keep.sum() < MIN_POINTS // 2:
            break
        a, b = moved[keep], dst[j[keep]]
        ca, cb = a.mean(0), b.mean(0)
        hm = (a - ca).T @ (b - cb)
        dth = math.atan2(hm[0, 1] - hm[1, 0], hm[0, 0] + hm[1, 1])
        dr = _rot(dth)
        dt = cb - dr @ ca
        th, t = th + dth, dr @ t + dt
        rmse, share = float(np.sqrt((dist[keep] ** 2).mean())), float(keep.mean())
        if abs(dth) < 1e-4 and np.hypot(*dt) < 1e-4:
            break
    return th, float(t[0]), float(t[1]), rmse, share


def _no_slide(key: np.ndarray, tx: float, tz: float) -> tuple[float, float]:
    """A key scan that is nearly one straight line (one flat wall) cannot tell a slide along that line: drop that part
    of the move (keep the part toward / away from the wall)."""
    ev, vec = np.linalg.eigh(np.cov(key.T))
    if ev[1] <= 0 or ev[0] / ev[1] > 0.04:
        return tx, tz
    u = vec[:, 1]   # the line's direction
    along = tx * u[0] + tz * u[1]
    return tx - along * u[0], tz - along * u[1]


class Odometry:
    """Camera pose from the ToF frames. update() at most `hz` times a second; pose() any time."""

    def __init__(self, start: dict | None = None, hz: float = 15.0) -> None:
        self.x, self.y, self.heading = (start or {}).get("x", 0.0), (start or {}).get("y", 0.0), (start or {}).get("heading", 0.0)
        self.key = None            # key scan points
        self.key_pose = None       # (x, y, heading) of the key scan
        self.rel = (0.0, 0.0, 0.0)
        self.quality = 0.0         # share of matched points of the last accepted match (0 = no match yet / lost)
        self.ms = 0.0
        self.matched = self.rejected = 0
        self.every = 1.0 / hz
        self.at = 0.0

    def reset(self, start: dict | None = None) -> None:
        self.__init__(start, 1.0 / self.every)

    def pose(self) -> dict:
        return {"x": self.x, "y": self.y, "heading": self.heading % 360, "quality": self.quality}

    def update(self, depth: np.ndarray, valid: np.ndarray) -> dict:
        now = time.monotonic()
        if now - self.at < self.every:
            return self.pose()
        self.at = now
        t0 = time.perf_counter()
        pts = scan_points(depth, valid)
        if len(pts) < MIN_POINTS:
            self.quality = 0.0
        elif self.key is None:
            self._new_key(pts)
        else:
            th, tx, tz, rmse, share = icp(pts, self.key, self.rel)
            if rmse > MAX_RMSE_M or share < MIN_INLIERS:
                self.rejected += 1
                self.quality = 0.0
                self._new_key(pts)   # lost: start again from here (the pose keeps its last value)
            else:
                self.matched += 1
                tx, tz = _no_slide(self.key, tx, tz)
                self.rel, self.quality = (th, tx, tz), share
                kx, ky, kh = self.key_pose
                h = math.radians(kh)
                self.x = kx + (tx * math.cos(h) + tz * math.sin(h)) * 1000
                self.y = ky + (-tx * math.sin(h) + tz * math.cos(h)) * 1000
                self.heading = kh - math.degrees(th)
                if math.hypot(tx, tz) > KEY_MOVE_M or abs(math.degrees(th)) > KEY_TURN_DEG:
                    self._new_key(pts)
        self.ms = (time.perf_counter() - t0) * 1000
        return self.pose()

    def _new_key(self, pts: np.ndarray) -> None:
        self.key, self.key_pose, self.rel = pts, (self.x, self.y, self.heading), (0.0, 0.0, 0.0)


class OdometryWorker:
    """Runs Odometry.update on its own thread with the newest frame only, so a slow match (the Pi) never delays the
    camera loop: submit() returns at once, pose() is the latest estimate (a frame or two behind while moving)."""

    def __init__(self, odo: Odometry, stop: threading.Event) -> None:
        self.odo, self.stop = odo, stop
        self.cond = threading.Condition()
        self.job = None
        threading.Thread(target=self._run, daemon=True, name="odometry").start()

    def submit(self, depth: np.ndarray, valid: np.ndarray) -> None:
        with self.cond:
            self.job = (depth, valid)   # an older frame not started yet is simply replaced
            self.cond.notify()

    def pose(self) -> dict:
        return self.odo.pose()

    def _run(self) -> None:
        while not self.stop.is_set():
            with self.cond:
                self.cond.wait_for(lambda: self.job is not None or self.stop.is_set(), timeout=0.5)
                job, self.job = self.job, None
            if job is not None:
                self.odo.update(*job)

    # what /api/odometry reads
    @property
    def ms(self) -> float:
        return self.odo.ms

    @property
    def matched(self) -> int:
        return self.odo.matched

    @property
    def rejected(self) -> int:
        return self.odo.rejected
