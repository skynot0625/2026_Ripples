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


def radial_coordinates(xy):
    """Zero is the innermost visible strip; the empty central shaft has no pixels."""
    xy = np.asarray(xy)
    center = (xy.min(axis=0)+xy.max(axis=0))/2
    r = np.linalg.norm(xy-center, axis=-1)
    return (r-r.min())/max(float(np.ptp(r)), 1e-9)


def pulse_field(height, age, direction="up", radius=None):
    h = np.clip(np.asarray(height), 0, 1)
    r = np.zeros_like(h) if radius is None else np.clip(np.asarray(radius), 0, 1)
    if direction == "up":
        # Concave arrival time accelerates upward; an opening cone grows brighter.
        local = age-(SWEEP*np.sqrt(h)+.20*r)
        width = .12+1.02*h**.8
        aperture = smooth((width-r)/.16)
        gain = .12+.88*h**1.4
        return (aperture*gain*smooth(local/.2)*np.exp(-np.maximum(local-.2, 0)/.95)
                *(1-smooth((local-2.2)/1.2)))
    delay = h if direction == "up_flat" else 1-h
    local = age-SWEEP*delay
    return smooth(local/.2)*np.exp(-np.maximum(local-.2, 0)/.95)*(1-smooth((local-2.4)/1.2))


def composite(old, target, height, age, direction="up", weights=None, radius=None):
    if direction not in ("up", "down", "up_flat"):
        raise ValueError("direction must be up, down or up_flat")
    target = limit_rgb(np.asarray(target)*PEAK/255., weights)
    if age >= DURATION:
        return target
    pulse = pulse_field(height, age, direction, radius)
    mix = smooth(age/2.6)
    base = np.asarray(old)*(1-mix)+target*mix
    return limit_rgb(base*(1-.45*pulse[..., None])+TINT*pulse[..., None], weights)


class StateTransition:
    def __init__(self, height, weights=None, direction="up", radius=None):
        if direction not in ("up", "down", "up_flat"):
            raise ValueError("direction must be up, down or up_flat")
        self.height, self.weights, self.direction = np.asarray(height), weights, direction
        self.radius = radius
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
            result = composite(self.origin, rgb, self.height, self.age, self.direction, self.weights, self.radius)
            self.age += dt
        else:
            result = limit_rgb(np.asarray(rgb)*PEAK/255., self.weights)
        self.previous = result.copy()
        return result.astype(np.uint8)
