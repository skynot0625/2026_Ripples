"""Two display states over unchanged reservoir dynamics.

XY is receiver position. Height is a labelled sensor-feature axis, NOT a claim
of reconstructed world height (camera pose and ceiling sensor extrinsics absent).
"""
import csv

import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates

from ripples.hardware.transport import VALID_MASK
from ripples.waves import SpikeWaves
from ripples.transition import StateTransition, radial_coordinates
from ripples.paths import LAYOUT_DIR

SIZE = 32
HEIGHT = 24
SLEEP_MEMORY_GAIN = .72
DREAM_TAU_S = 3.2
ACTIVITY_DECAY_TAU_S = 1.5
DISTANCE_RANGE_M = 4.
BANDS = (("low", .46, (0, 0, 255)),
         ("mid", .55, (0, 0, 255)),
         ("high", .64, (0, 0, 255)))


def frequency_rate(hz):
    """Spectral centroid (not pitch): 200..4000 Hz -> 0.15..0.65 visual Hz."""
    return .15+.5*np.clip(np.log(np.maximum(hz, 200)/200)/np.log(20), 0, 1)


def sound_height(hz):
    """Spectral brightness maps upwards inside the sound layer; not fundamental pitch."""
    return .47+.14*np.clip(np.log(np.maximum(hz,200)/200)/np.log(20),0,1)


def layer_separation(height):
    """Dim only the two display separators, including their firing background."""
    h = np.asarray(height)
    gain = np.ones_like(h, dtype=float)
    for low, high in ((.32,.40),(.68,.76)):
        a = np.clip((h-low)/.015,0,1)
        b = np.clip((high-h)/.015,0,1)
        gain *= 1-.90*(a*a*(3-2*a))*(b*b*(3-2*b))
    return gain


class RipplesVisual:
    def __init__(self, state="activate", *, criticality="activity", dream_strength=.45, seed=120, transition_direction="up", output_limits=True):
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
        self.trail_frequency = np.zeros((SIZE, SIZE, 2), np.float64)
        self.memory_frequency = np.zeros_like(self.trail_frequency)
        self.frequency_phase = np.zeros((SIZE, SIZE), np.float64)

        # Weight model samples by the number of physical pixels on their strip.
        weights = np.ones(VALID_MASK.shape, dtype=float)
        with (LAYOUT_DIR/"mapping_plus10.csv").open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row["included_in_layout"] == "True":
                    z, p = int(row["zone"])-1, int(row["port_c"])-1
                    old = int(VALID_MASK[z,p].sum())
                    source = np.rint(np.linspace(0, old-1, int(row["led_count"])-1)).astype(int)
                    weights[z,p,:old] = np.bincount(source, minlength=old)
        self.transition = StateTransition(self.coords[2]/(HEIGHT-1), weights[VALID_MASK], transition_direction, radial_coordinates(xyz[:, :2]))
        self.output_limits = output_limits
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
            # Expanded middle band now belongs to sound; keep three separate layers.
            z = z*(.3625/.40) if z < .40 else (.55+1.25*(z-.55) if z < .70 else .7375+(z-.70)*(.2625/.30))
            zi = int(round(np.clip(z, 0, 1)*(HEIGHT-1)))
            if 0 <= x < SIZE and 0 <= y < SIZE:
                if color[2] and not color[0] and not color[1]:
                    # Fractional-height deposition avoids one-voxel jumps in the sound ribbon.
                    position=float(np.clip(z,0,1)*(HEIGHT-1));lo=int(np.floor(position));fraction=position-lo
                    for index,weight in ((lo,1-fraction),(min(lo+1,HEIGHT-1),fraction)):
                        volume[x,y,index]=np.maximum(volume[x,y,index],np.asarray(color)*strength*weight)
                else:
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
                point = np.array([(i%3-1)*1.7, (1-i//3)*1.7, .08+.23*u])
                level = np.sqrt(coverage)*(.40+.60*(1-d/DISTANCE_RANGE_M))
                points[i] = (point, level)
                # Anchor each valid sector to the floor, with a brighter endpoint.
                segment(np.array([point[0], point[1], .02]), point, (0, 255, 0), .32*level)
                dot(*point, (0, 255, 0), level)
            # A coherent 3x3 wire surface; don't bridge across a missing sector.
            for i, (point, level) in points.items():
                for j in ((i+1 if i%3 < 2 else -1), i+3):
                    if j in points:
                        other, other_level = points[j]
                        segment(point, other, (0, 255, 0), .48*min(level, other_level))
            # One measured upward ray makes one local flat patch, not invented depths.
            # Adjacent observed XY patches form a ceiling; amplify small example relief.
            if f.get("ceiling_0", 0) > 0:
                length = .025+.20*(.5+.5*np.tanh((float(f["ceiling_0"])-2.2)/.10))
                z = .98-length
                for dx in (-2., -1., 0., 1., 2.):
                    segment(np.array([dx, -2., z]), np.array([dx, 2., z]), (255, 0, 0), .80)
                for dx in (-2., 2.):
                    segment(np.array([dx, 0., .98]), np.array([dx, 0., z]), (255, 0, 0), .12)
        elif event["source"] == "maek":
            # L/C/R levels remain channel identities, never invented spectral bands.
            palette = ((0, 0, 255), (0, 0, 255), (0, 0, 255))
            sound_points={}
            for i in range(3):
                level_value=f.get(f'mic_level_{i}')
                if level_value is None and f.get(f'power_{i}',0)>0:
                    level_value=(10*np.log10(f[f'power_{i}'])+70)/50
                level = float(np.clip(level_value or 0, 0, 1))
                if level > 0:
                    x=(i-1)*1.7
                    hz=f.get(f'centroid_{i}',0)
                    if np.isfinite(hz) and hz>0:
                        center=float(sound_height(hz));half=.010+.018*np.sqrt(level)
                        segment(np.array([x,0.,center-half]),np.array([x,0.,center+half]),palette[i],.55*np.sqrt(level))
                        segment(np.array([x-.55,0.,center]),np.array([x+.55,0.,center]),palette[i],np.sqrt(level))
                        sound_points[i]=(np.array([x,0.,center]),np.sqrt(level))
                    else:
                        # A level-only packet carries no evidence of pitch/frequency.
                        top=.46+.18*level
                        segment(np.array([x, 0., .44]), np.array([x, 0., top]), palette[i], .65*np.sqrt(level))
                        dot(x, 0, top, palette[i], np.sqrt(level))
            for i,(point,level) in sound_points.items():
                if i+1 in sound_points:
                    other,other_level=sound_points[i+1]
                    segment(point,other,(0,0,255),.55*min(level,other_level))
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

    @staticmethod
    def stamp_frequency(field, event):
        if event['source'] != 'maek':
            return
        values = [event['features'].get(f'centroid_{i}') for i in range(3)]
        values = [v for v in values if v is not None and np.isfinite(v) and v > 0]
        if not values:
            return  # Levels alone cannot determine frequency.
        xy = (np.asarray(event['xy'])+5)/10*(SIZE-1)
        if np.any(xy < 0) or np.any(xy > SIZE-1):
            return
        x, y = np.indices((SIZE, SIZE))
        weight = np.exp(-((x-xy[0])**2+(y-xy[1])**2)/(2*2.2**2))
        weight[weight < .001] = 0
        field[..., 0] += weight*np.mean(values)
        field[..., 1] += weight

    def set_memory(self, observations):
        self.memory.fill(0)
        self.memory_frequency.fill(0)
        high, low = np.zeros_like(self.memory), np.zeros_like(self.memory)
        for event in observations:
            self.stamp(self.memory, event)
            self.stamp_frequency(self.memory_frequency, event)
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

    def render(self, raw, spike_count=None, *, events=(), dt=.125, sensor_mask=None):
        if not np.isfinite(dt) or dt <= 0:
            raise ValueError("dt must be positive")
        # Seconds, not frame counts: trail lifetime remains stable at 8 or 10 FPS.
        self.trail *= np.exp(-dt/.9)
        self.trail_frequency *= np.exp(-dt/.9)
        self.trail[self.trail < .5] = 0
        for event in events:
            self.stamp(self.trail, event)
            self.stamp_frequency(self.trail_frequency, event)
        # Identical spike display in the direct/slow comparison. Only the extra
        # radial layer differs; the base firing pixels keep the same color/decay.
        self.activity *= np.exp(-dt/ACTIVITY_DECAY_TAU_S)
        self.activity = np.maximum(self.activity, raw.astype(np.float32))
        if self.criticality == "activity":
            wave = self.activity
        elif self.criticality == "slow_waves":
            radial = self.waves.render(raw, spike_count=spike_count, dt=dt, include_spikes=False)
            wave = self.activity+(255-self.activity)*(radial/255.)
        else:
            wave = self.waves.render(raw, spike_count=spike_count, dt=dt)
        alpha = np.exp(-dt/DREAM_TAU_S)
        self.dream_noise = alpha*self.dream_noise + np.sqrt(1-alpha*alpha)*self.rng.standard_normal(self.dream_noise.shape)
        noise = np.tanh(3.8*gaussian_filter(self.dream_noise, sigma=1.1))[..., None]
        volume = (self._smooth(self.trail) if self.state == "activate" else
                  np.clip(SLEEP_MEMORY_GAIN*self.memory+self.dream_strength*self.memory_spread*noise, 0, 255))
        colors = .85*np.stack([map_coordinates(volume[..., i], self.coords, order=1, mode="nearest")
                               for i in range(3)], axis=-1)
        if self.state == "activate":
            colors *= 1.15  # Sensory observations stay brighter than the firing background.
        # Disjoint feature-height ranges prevent interpolation tails from changing identity.
        h = self.coords[2]/(HEIGHT-1)
        colors[:,0] *= np.clip((h-.76)/.02, 0, 1)
        # Keep interpolation/std tails out of the camera/sound separation gap.
        colors[:,1] *= np.clip((.32-h)/.04, 0, 1)
        colors[:,2] *= np.clip((h-.40)/.03, 0, 1)*np.clip((.68-h)/.04,0,1)
        field = self.trail_frequency if self.state == 'activate' else self.memory_frequency
        hz = np.divide(field[...,0], field[...,1], out=np.zeros((SIZE,SIZE)), where=field[...,1] > 1e-6)
        self.frequency_phase = (self.frequency_phase+2*np.pi*frequency_rate(hz)*dt) % (2*np.pi)
        envelope = np.where(hz > 0, .72+.28*np.sin(self.frequency_phase), 1.)
        colors[:,2] *= map_coordinates(envelope, self.coords[:2], order=1, mode='nearest')
        if sensor_mask is not None:
            mask = np.asarray(sensor_mask)
            if mask.shape != colors.shape or not np.isfinite(mask).all() or np.any((mask < 0)|(mask > 1)):
                raise ValueError('sensor mask must match logical sensor channels and be in [0,1]')
            colors *= mask  # Criticality below is intentionally not masked or slowed.
        sensor_strength = np.max(colors, axis=1)
        sensor_kind = np.argmax(colors, axis=1)
        palette = np.full((3,3), 220., dtype=float)  # Both states are neutral white.
        colors = sensor_strength[:,None]*palette[sensor_kind]/255.
        result = np.zeros((*VALID_MASK.shape, 3), np.uint8)
        if self.state in ("activate", "sleep"):
            base = wave[VALID_MASK].astype(np.float32)
            # The fixed mean field keeps exactly the same encoding as Activate.
            # Model-driven waves are a separate screen-blended layer: no trails,
            # count-dependent gain or re-encoding of the spatial statistics.
            firing = base.max(axis=1)/255.
            if self.state == "activate":
                firing *= .12  # All neurons remain visible; contrast is controlled only by luminance.
            # Keep sensor hue fixed in both states; empty points use their feature-band hue.
            tint = palette[sensor_kind]/255.
            empty = sensor_strength < 1.
            region_kind = np.where(h >= .7375, 0, np.where(h >= .3625, 2, 1))
            tint[empty] = palette[region_kind[empty]]/255.
            energy = sensor_strength + (255-sensor_strength)*firing
            colors = energy[:,None]*tint
        logical = np.clip(colors, 0, 255)
        result[VALID_MASK] = (self.transition.render(logical, self.state, dt) if self.output_limits
                              else logical.astype(np.uint8))
        if self.output_limits:
            result[VALID_MASK] = (result[VALID_MASK]*layer_separation(h)[:,None]).astype(np.uint8)
        return result
