"""State-change light envelope and conservative RGB budgets (not a current sensor)."""
import numpy as np

DURATION = 5.0
SWEEP = 1.4
PEAK = 96.0
MEAN = 18.0
TINT = np.array([96., 84., 64.])


def smooth(x):
    x = np.clip(x, 0, 1)
    return x*x*(3-2*x)


def limit_rgb(rgb, weights=None):
    out = np.clip(np.asarray(rgb, dtype=float), 0, PEAK)
    mean = float(np.average(out.mean(axis=-1), weights=weights))
    if mean > MEAN:
        out *= MEAN/mean
    return out


def composite(old, target, height, age, direction="up", weights=None):
    if direction not in ("up", "down"):
        raise ValueError("direction must be up or down")
    target = limit_rgb(np.asarray(target)*PEAK/255., weights)
    if age >= DURATION:
        return target
    delay = np.asarray(height) if direction == "up" else 1-np.asarray(height)
    local = age-SWEEP*delay
    pulse = smooth(local/.2)*np.exp(-np.maximum(local-.2, 0)/.95)*(1-smooth((local-2.4)/1.2))
    mix = smooth(age/2.6)
    base = np.asarray(old)*(1-mix)+target*mix
    return limit_rgb(base*(1-.45*pulse[..., None])+TINT*pulse[..., None], weights)


class StateTransition:
    def __init__(self, height, weights=None, direction="up"):
        if direction not in ("up", "down"):
            raise ValueError("direction must be up or down")
        self.height, self.weights, self.direction = np.asarray(height), weights, direction
        self.state = None
        self.age = DURATION
        self.previous = None
        self.origin = None

    def render(self, rgb, state, dt):
        if state not in ("activate", "sleep") or not np.isfinite(dt) or dt <= 0:
            raise ValueError("invalid transition state/dt")
        if self.state is not None and state != self.state:
            self.origin = self.previous.copy()
            self.age = 0.0
        self.state = state
        if self.age < DURATION:
            result = composite(self.origin, rgb, self.height, self.age, self.direction, self.weights)
            self.age += dt
        else:
            result = limit_rgb(np.asarray(rgb)*PEAK/255., self.weights)
        self.previous = result.copy()
        return result.astype(np.uint8)
