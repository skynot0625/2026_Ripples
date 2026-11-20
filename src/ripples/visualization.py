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

        if event["source"] == "hyeol":
            for i in range(9):
                key = f"depth_{i}"
                if key in f:
                    coverage = np.clip(f.get(f"coverage_{i}", 0.), 0., 1.)
                    if coverage <= 0 or f[key] <= 0:
                        continue
                    # Cyan distance relief; 3x3 sectors stay around the receiver.
                    z = .12+.70*np.clip(f[key]/DISTANCE_RANGE_M, 0, 1)
                    dx, dy = (i%3-1)*1.3, (1-i//3)*1.3
                    level = .22+.78*np.sqrt(coverage)
                    # Dim stems with bright tops read as a local depth relief.
                    for stem_z in np.arange(.08, z, 1/(HEIGHT-1)):
                        dot(dx, dy, stem_z, (35, 210, 255), .12*level)
                    dot(dx, dy, z, (35, 210, 255), level)
            # One upward-facing point, exactly on the instrument XY axis.
            if "ceiling_0" in f and f["ceiling_0"] > 0:
                z = .12+.70*np.clip(f["ceiling_0"]/DISTANCE_RANGE_M, 0, 1)
                for stem_z in np.arange(.08, z, 1/(HEIGHT-1)):
                    dot(0, 0, stem_z, (155, 110, 255), .18)
                dot(0, 0, z, (190, 140, 255), .95)
        elif event["source"] == "maek":
            # Center /api/maek reports normalized levels, not a spectrum or dBFS.
            # Keep their units and identity separate from PCM frequency bands.
            for i in range(3):
                key = f"mic_level_{i}"
                if key in f:
                    dot((i-1)*1.3, 0, .10, (240, 225, 180), np.clip(f[key], 0, 1))
            for name, z, color in BANDS:
                key = f"band_{name}"
                if key not in f:
                    continue
                # Power is averaged in linear units; dBFS is only a display transfer.
                dbfs = 10*np.log10(max(f[key], 1e-12))
                level = np.clip((dbfs+60)/54, 0, 1)
                dot(0, 0, z, color, level)

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
