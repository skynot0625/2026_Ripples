from ripples.paths import LAYOUT_DIR
"""Render spike flashes, persistence and expanding wave fronts on the hanging strips."""
import csv
from pathlib import Path

import numpy as np

from ripples.hardware.transport import VALID_MASK


class SpikeWaves:
    def __init__(self, time_scale=1.):
        if not np.isfinite(time_scale) or time_scale <= 0:
            raise ValueError("time_scale must be positive")
        self.time_scale = time_scale
        self.light = np.zeros(VALID_MASK.shape, np.float32)
        self.xyz = np.zeros((*VALID_MASK.shape, 3), np.float32)
        with (LAYOUT_DIR / "mapping_plus10.csv").open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row["included_in_layout"] != "True":
                    continue
                z, p = int(row["zone"])-1, int(row["port_c"])-1
                n = int(VALID_MASK[z,p].sum())
                self.xyz[z,p,:n,0] = float(row["x"])
                self.xyz[z,p,:n,1] = float(row["y"]) * np.cos(np.deg2rad(28.76))
                # Depth from the common bottom plane in units of strip spacing.
                self.xyz[z,p,:n,2] = np.linspace(int(row["visible_led_count"])-1, 0, n) * (5/12)

        # The central shaft has no LEDs. Start at the nearest visible surface
        # of a sphere centred on the installation axis at mid-height.
        height = self.xyz[...,2][VALID_MASK]
        center = np.array([0., 0., (height.min()+height.max())/2], dtype=np.float32)
        delta = self.xyz - center
        distance = np.linalg.norm(delta, axis=-1)
        visible_radius = np.maximum(distance - distance[VALID_MASK].min(), 0.)
        x, y, z = np.moveaxis(delta, -1, 0)
        bend = .40*np.sin(.40*x+.24*z)*np.sin(.37*y-.20*z)
        self.travel = visible_radius + bend*np.minimum(visible_radius/4., 1.)
        self.core = np.exp(-.5*(visible_radius/.95)**2)
        self.baseline = 34.
        self.previous_count = 0
        self.burst_peak = 0
        self.fronts = []

    def render(self, frame, spike_count=None, dt=.1, include_spikes=True):
        if not np.isfinite(dt) or dt <= 0:
            raise ValueError("dt must be positive")
        fired = frame[...,2] > 0
        count = int(fired.sum()) if spike_count is None else spike_count
        threshold = max(95., self.baseline*2.35)
        if self.burst_peak and count < max(self.baseline*1.25, self.burst_peak*.42):
            self.burst_peak = 0
        if not self.burst_peak and count >= threshold and count > self.previous_count:
            self.fronts = self.fronts[-2:] + [dict(age=0, strength=0.)]
            self.burst_peak = count
        if self.burst_peak:
            self.burst_peak = max(self.burst_peak, count)
            # A spike surge following a precursor intensifies the same source immediately.
            self.fronts[-1]["strength"] = min(1., .32 + .27*np.log2(max(1., self.burst_peak/threshold)))
        self.previous_count = count
        self.baseline += .065*(min(count, 2.5*self.baseline)-self.baseline)

        self.light *= .70**(dt*10)
        self.light[fired] = 1.
        light = self.light
        red = 200*light**1.6
        green = 104*light**3
        blue = 150*light
        if not include_spikes:
            red, green, blue = np.zeros_like(light), np.zeros_like(light), np.zeros_like(light)
        for front in self.fronts:
            age, energy = front["age"], front["strength"]
            # A local bloom opens before the first crest leaves it.
            bloom = energy * (.75 + .25*min(age,1)) * np.exp(-.16*age) * self.core
            speed = .72 + .42*energy
            radius = speed * (age - 1.8*(1 - np.exp(-age/1.8)))
            offset = self.travel - radius
            width = .72 + .023*radius
            crest = np.exp(-.5*(offset/width)**2)
            wake = np.exp(-.5*((offset+2.8)/(width*.94))**2)
            echo = np.exp(-.5*((offset+5.4)/width)**2)
            fade = energy*(1-np.exp(-age*.55))*np.exp(-.075*age)
            red += 250*bloom + fade*(35*crest + 66*echo)
            green += 212*bloom + fade*(188*crest + 45*wake)
            blue += 92*bloom + fade*(208*crest + 159*wake + 88*echo)
            front["age"] += dt*10*self.time_scale
        self.fronts = [front for front in self.fronts if front["age"] < 30 or
                       (self.burst_peak and front is self.fronts[-1])]
        rgb = np.stack((red, green, blue), axis=-1)
        rgb[~VALID_MASK] = 0
        return np.clip(rgb, 0, 255).astype(np.uint8)
