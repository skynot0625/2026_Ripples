"""Click-free output: fades around gaps and slow drift correction.

Teensy (mic clock) and the Pi's I2S output run on independent clocks, and USB
packets can be dropped. Writing blocks straight to the speaker then produces
hard waveform jumps (clicks / crackle) at every gap, reset, or inserted
silence. This module:

1. holds back one block so it can be faded out when the next block turns out
   to be discontinuous (or does not arrive in time);
2. fades in the first block after any gap/reset, so filter-reset transients
   are inaudible;
3. resamples by a tiny ratio (at most +-0.5 %) steered by the input queue
   fill, so a faster or slower Teensy clock no longer forces packet drops
   or underruns.
"""
import numpy as np


def _ramp(frames, rising):
    t = (np.arange(frames, dtype=np.float32) + 0.5) / frames
    ramp = 0.5 - 0.5 * np.cos(np.pi * t)
    return (ramp if rising else ramp[::-1])[:, None].astype(np.float32)


class Declicker:
    """Feed processed stereo blocks in order; returns blocks safe to play."""

    def __init__(self):
        self.held = None
        self.fading_in = True
        self.fades = 0

    def push(self, stereo, gap=False):
        stereo = np.asarray(stereo, dtype=np.float32)
        out = []
        if gap:
            out.extend(self.fade_out())
        if self.fading_in:
            stereo = stereo * _ramp(len(stereo), True)
            self.fading_in = False
        if self.held is not None:
            out.append(self.held)
        self.held = stereo
        return out

    def fade_out(self):
        """Input stopped or jumps: release the held block faded to silence."""
        self.fading_in = True
        if self.held is None:
            return []
        block, self.held = self.held, None
        self.fades += 1
        return [block * _ramp(len(block), False)]


class DriftResampler:
    """Linear-interpolation resampler with a slowly varying ratio.

    ratio = output frames / input frames. At exactly 1.0 the signal passes
    unchanged (one frame of delay). The fractional position is carried across
    blocks so block boundaries are seamless.
    """

    def __init__(self, channels=2):
        self.prev = np.zeros(channels, dtype=np.float64)
        self.pos = 0.0

    def reset(self):
        self.prev[:] = 0
        self.pos = 0.0

    def process(self, block, ratio):
        x = np.asarray(block, dtype=np.float64)
        n = len(x)
        if n == 0:
            return x.astype(np.float32)
        step = 1.0 / ratio
        src = np.vstack((self.prev[None, :], x))  # index 0 = last frame of previous block
        count = max(0, int(np.ceil((n - self.pos) / step)))
        t = self.pos + step * np.arange(count)
        t = t[t < n]
        i = t.astype(np.int64)
        f = (t - i)[:, None]
        y = src[i] * (1 - f) + src[i + 1] * f
        self.pos = (t[-1] + step - n) if len(t) else (self.pos - n)
        self.prev = x[-1].copy()
        return y.astype(np.float32)


class DriftController:
    """Turns input-queue fill (packets) into a resampling ratio.

    More packets waiting than the target means the mic clock is faster than
    the speaker clock: play slightly fewer output frames per input frame.
    """

    def __init__(self, target_packets=6.0, gain=0.0005, limit=0.005, smoothing=0.01):
        self.target = target_packets
        self.gain = gain
        self.limit = limit
        self.smoothing = smoothing
        self.level = target_packets

    def update(self, queued_packets):
        self.level += self.smoothing * (queued_packets - self.level)
        correction = self.gain * (self.level - self.target)
        return 1.0 - float(np.clip(correction, -self.limit, self.limit))
