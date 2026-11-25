"""480 s chronological spatial recall + 120 s full memory. No neural time scaling.

Tracks are bounded position summaries (seconds, normalized tower X/Y in -5..5).
The mask selects existing mean/std features, never synthesizes measurements.
"""
import numpy as np

REPLAY_SECONDS = 480.
SLEEP_SECONDS = 600.


def three_phase_preview(elapsed):
    """One minute per display mode; do not accelerate the firing clip or audio."""
    if not np.isfinite(elapsed) or elapsed < 0 or elapsed >= 180:
        return None
    if elapsed < 60:
        return 'activate', elapsed, elapsed, '1/3 ACTIVE'
    if elapsed < 120:
        return 'sleep', (elapsed-60)*8, elapsed, '2/3 SLEEP TRACK'
    return 'sleep', 480+(elapsed-120)*2, elapsed, '3/3 SLEEP ALL'


class SleepReplay:
    def __init__(self, tracks):
        self.tracks = {}
        for role in ('hyeol', 'maek'):
            rows = np.asarray(tracks.get(role, []), dtype=float)
            if not rows.size:
                rows = np.empty((0,3))
            if rows.ndim != 2 or rows.shape[1] not in (3,4) or len(rows) > 9600 or not np.isfinite(rows).all():
                raise ValueError('invalid or unbounded sleep track')
            if len(rows) and (np.any(np.diff(rows[:,0]) <= 0) or np.any(abs(rows[:,1:3]) > 5)):
                raise ValueError('unordered or out-of-bounds sleep track')
            self.tracks[role] = rows
        nonempty = [rows for rows in self.tracks.values() if len(rows)]
        self.start = min((r[0,0] for r in nonempty), default=0.)
        self.end = max((r[-1,0] for r in nonempty), default=0.)

    @staticmethod
    def stage(seconds):
        return 'track' if seconds % SLEEP_SECONDS < REPLAY_SECONDS else 'all'

    def heads(self, role, seconds):
        """Six seconds of soft visual trail; do not bridge missing tracking intervals."""
        local = seconds % SLEEP_SECONDS
        rows = self.tracks[role]
        result = []
        if not len(rows):
            return result
        for age in np.arange(0., 6.01, .5):
            if age > local:
                continue
            target = self.start+(self.end-self.start)*min(1., (local-age)/REPLAY_SECONDS)
            if target < rows[0,0] or target > rows[-1,0]:
                continue
            j = int(np.searchsorted(rows[:,0], target, side='right')-1)
            point = rows[j,1:3]
            if j+1 < len(rows):
                gap = rows[j+1,0]-rows[j,0]
                connected = gap <= 3. if rows.shape[1] == 3 else rows[j,3] == rows[j+1,3]
                if not connected and target > rows[j,0]+1.:
                    continue
                if connected:
                    u = (target-rows[j,0])/gap
                    point = point*(1-u)+rows[j+1,1:3]*u
            result.append((point, float(np.exp(-age/2.5))))
        return result

    def mask(self, xy, seconds):
        xy = np.asarray(xy, dtype=float)
        local = seconds % SLEEP_SECONDS
        if local >= REPLAY_SECONDS:
            return np.ones((len(xy),3))
        masks = []
        # A three-second reveal completes exactly at the eight-minute boundary.
        u = float(np.clip((local-477.)/3., 0, 1))
        reveal = u*u*(3-2*u)
        for role in ('hyeol','maek'):
            strength = np.zeros(len(xy))
            for point, weight in self.heads(role, seconds):
                distance = ((xy-point)**2).sum(axis=1)
                strength = np.maximum(strength, weight*np.exp(-distance/(2*.70**2)))
            masks.append(strength*(1-reveal)+reveal)
        return np.stack([masks[0],masks[0],masks[1]], axis=1)


def example_sleep_data(events, snapshot):
    tracks = {role: [] for role in ('hyeol','maek')}
    last_second = {}
    for event in sorted(events, key=lambda e:e['t']):
        role, second = event['source'], int(event['t'])
        if last_second.get(role) == second:
            continue
        last_second[role] = second
        tracks[role].append([float(event['t']), *event['xy']])
    return {'schema':'ripples.sleep-replay/1', 'example':True,
            'replay_s':480, 'all_s':120, 'position_rate_hz':1,
            'tracks':{role:rows[-1200:] for role,rows in tracks.items()},
            'snapshot':snapshot,
            'description':'Chronological positions select stored mean/std; criticality runs at normal display speed.'}
