"""Two display states over unchanged reservoir dynamics.

XY is receiver position. Height is a labelled sensor-feature axis, NOT a claim
of reconstructed world height (camera pose and ceiling sensor extrinsics absent).
"""
import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates

from ripples.hardware.transport import VALID_MASK
from ripples.waves import SpikeWaves

SIZE = 32
HEIGHT = 24
DISTANCE_RANGE_M = 4.
BANDS = (("low", .18, (255, 140, 35)),
         ("mid", .46, (255, 70, 60)),
         ("high", .74, (225, 65, 185)))


class RipplesVisual:
    def __init__(self, state="activate", *, criticality="activity", dream_strength=.45, seed=120):
        if criticality not in ("activity", "slow_waves", "legacy_waves"):
            raise ValueError("unknown criticality display mode")
        if not np.isfinite(dream_strength) or not 0 <= dream_strength <= 1:
            raise ValueError("dream_strength must be in [0,1]")
        self.criticality = criticality
        self.dream_strength = dream_strength
        self.waves = SpikeWaves(time_scale=.30 if criticality == "slow_waves" else 1.)
        xyz = self.waves.xyz[VALID_MASK]
        low, high = xyz.min(axis=0), xyz.max(axis=0)
        self.coords = ((xyz-low)/(high-low)*[SIZE-1, SIZE-1, HEIGHT-1]).T
        self.trail = np.zeros((SIZE, SIZE, HEIGHT, 3), np.float32)
        self.memory = np.zeros_like(self.trail)
        self.memory_spread = np.zeros_like(self.trail)
        self.rng = np.random.default_rng(seed)
        self.dream_noise = self.rng.standard_normal(self.trail.shape[:-1]).astype(np.float32)
        self.activity = np.zeros((*VALID_MASK.shape, 3), np.float32)
        self.set_state(state)

    def set_state(self, state):
        if state not in ("activate", "sleep"):
            raise ValueError("state must be activate or sleep")
        self.state = state

    @staticmethod
    def stamp(volume, event):
        xy = (np.asarray(event["xy"])+5)/10*(SIZE-1)
        if np.any(xy < 0) or np.any(xy > SIZE-1):
            return
        f = event["features"]

        def dot(dx, dy, z, color, strength):
            x, y = np.rint(xy+[dx, dy]).astype(int)
            zi = int(round(np.clip(z, 0, 1)*(HEIGHT-1)))
            if 0 <= x < SIZE and 0 <= y < SIZE:
                volume[x, y, zi] = np.maximum(volume[x, y, zi], np.asarray(color)*strength)

        def segment(a, b, color, strength):
            # Sample a short feature line on the display grid, not a travelling wave.
            steps = max(2, int(max(abs(a[0]-b[0]), abs(a[1]-b[1]), abs(a[2]-b[2])*(HEIGHT-1))*2)+1)
            for u in np.linspace(0, 1, steps):
                dot(*(a*(1-u)+b*u), color, strength)

        if event["source"] == "hyeol":
            points = {}
            for i in range(9):
                key = f"depth_{i}"
                coverage = float(np.clip(f.get(f"coverage_{i}", 0.), 0, 1))
                if key not in f or f[key] <= 0 or coverage <= 0:
                    continue
                d = float(np.clip(f[key], 0, DISTANCE_RANGE_M))
                # Fixed logarithmic distance axis gives near obstacles more resolution.
                u = np.log1p(d/.35)/np.log1p(DISTANCE_RANGE_M/.35)
                point = np.array([(i%3-1)*1.7, (1-i//3)*1.7, .12+.46*u])
                level = np.sqrt(coverage)*(.40+.60*(1-d/DISTANCE_RANGE_M))
                points[i] = (point, level)
                dot(*point, (25, 220, 255), level)
            # A coherent 3x3 wire surface; don't bridge across a missing sector.
            for i, (point, level) in points.items():
                for j in ((i+1 if i%3 < 2 else -1), i+3):
                    if j in points:
                        other, other_level = points[j]
                        segment(point, other, (25, 220, 255), .48*min(level, other_level))
            # An upper purple cap separates the one upward sensor from the camera.
            if f.get("ceiling_0", 0) > 0:
                z = .72+.22*float(np.clip(f["ceiling_0"]/DISTANCE_RANGE_M, 0, 1))
                segment(np.array([0., 0., .64]), np.array([0., 0., z]), (165, 95, 255), .28)
                for a, b in (((-1.7, 0, z), (1.7, 0, z)), ((0, -1.7, z), (0, 1.7, z))):
                    segment(np.array(a), np.array(b), (195, 125, 255), .95)
        elif event["source"] == "maek":
            # L/C/R levels remain channel identities, never invented spectral bands.
            palette = ((255, 155, 55), (255, 220, 145), (255, 125, 100))
            for i in range(3):
                level = float(np.clip(f.get(f"mic_level_{i}", 0), 0, 1))
                if level > 0:
                    x, top = (i-1)*1.7, .08+.38*level
                    segment(np.array([x, 0., .06]), np.array([x, 0., top]), palette[i], .65*np.sqrt(level))
                    dot(x, 0, top, palette[i], np.sqrt(level))
            for name, z, color in BANDS:
                key = f"band_{name}"
                if key not in f:
                    continue
                # PCM band powers stay linear in memory; fixed dBFS for display only.
                dbfs = 10*np.log10(max(f[key], 1e-12))
                level = float(np.clip((dbfs+60)/54, 0, 1))
                if level > 0:
                    half_width = .5+1.8*level
                    segment(np.array([-half_width, 0., z]), np.array([half_width, 0., z]), color, level)

    def set_memory(self, observations):
        self.memory.fill(0)
        high, low = np.zeros_like(self.memory), np.zeros_like(self.memory)
        for event in observations:
            self.stamp(self.memory, event)
            means, deviations = event["features"], event.get("std", {})
            for sign, target in ((1, high), (-1, low)):
                perturbed = {}
                for key, value in means.items():
                    std = deviations.get(key, 0.) if event.get("counts", {}).get(key, 0) > 1 else 0.
                    upper = 1. if key.startswith("coverage_") else np.inf
                    perturbed[key] = float(np.clip(value+sign*std, 0., upper))
                self.stamp(target, {**event, "features": perturbed})
        self.memory = self._smooth(self.memory)
        # Finite-difference propagation of feature std into the existing encoding.
        # Mean locations/colors are unchanged; uncertain points shimmer locally.
        self.memory_spread = np.minimum(90., np.maximum(np.abs(self._smooth(high)-self.memory),
                                                       np.abs(self._smooth(low)-self.memory)))

    @staticmethod
    def _smooth(volume):
        # Spread only in space, never mix RGB channels. Restore sparse peak visibility.
        return np.clip(gaussian_filter(volume, sigma=(.9, .9, .8, 0))*6.5, 0, 255)

    def render(self, raw, spike_count=None, *, events=(), dt=.125):
        if not np.isfinite(dt) or dt <= 0:
            raise ValueError("dt must be positive")
        # Seconds, not frame counts: trail lifetime remains stable at 8 or 10 FPS.
        self.trail *= np.exp(-dt/.9)
        self.trail[self.trail < .5] = 0
        for event in events:
            self.stamp(self.trail, event)
        # Identical spike display in the direct/slow comparison. Only the extra
        # radial layer differs; the base firing pixels keep the same color/decay.
        self.activity *= np.exp(-dt/.65)
        self.activity = np.maximum(self.activity, raw.astype(np.float32))
        if self.criticality == "activity":
            wave = self.activity
        elif self.criticality == "slow_waves":
            radial = self.waves.render(raw, spike_count=spike_count, dt=dt, include_spikes=False)
            wave = self.activity+(255-self.activity)*(radial/255.)
        else:
            wave = self.waves.render(raw, spike_count=spike_count, dt=dt)
        alpha = np.exp(-dt/1.8)
        self.dream_noise = alpha*self.dream_noise + np.sqrt(1-alpha*alpha)*self.rng.standard_normal(self.dream_noise.shape)
        noise = np.tanh(3*gaussian_filter(self.dream_noise, sigma=.75))[..., None]
        volume = (self._smooth(self.trail) if self.state == "activate" else
                  np.clip(self.memory+self.dream_strength*self.memory_spread*noise, 0, 255))
        colors = .85*np.stack([map_coordinates(volume[..., i], self.coords, order=1, mode="nearest")
                               for i in range(3)], axis=-1)
        result = np.zeros((*VALID_MASK.shape, 3), np.uint8)
        if self.state == "sleep":
            base = wave[VALID_MASK].astype(np.float32)
            # The fixed mean field keeps exactly the same encoding as Activate.
            # Model-driven waves are a separate screen-blended layer: no trails,
            # count-dependent gain or re-encoding of the spatial statistics.
            colors = colors+(255-colors)*(base/255.)
        result[VALID_MASK] = np.clip(colors, 0, 255).astype(np.uint8)
        return result
