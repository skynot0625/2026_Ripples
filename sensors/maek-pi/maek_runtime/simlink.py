"""Live values to the 심 server (POST /api/maek, ~10 times a second).

Format: docs/SIM_FORMAT.md, example: examples/sim_maek_sample.json.
Runs in its own thread with a one-slot mailbox, so a slow or missing server
never delays the audio loop; failed sends are dropped (live values only).
"""
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request

LOG = logging.getLogger('simlink')


def level01(rms, low_db=-70.0, high_db=-20.0):
    """Mic RMS (digital full scale) -> 0..1 on a dB scale. Not calibrated to SPL."""
    db = 20 * math.log10(max(float(rms), 1e-7))
    return min(1.0, max(0.0, (db - low_db) / (high_db - low_db)))


def payload(result, seq, device='mic', low_db=-70.0, high_db=-20.0):
    rms = (result.rms_left, result.rms_center, result.rms_right)
    return {
        'v': 1,
        'device': device,
        'seq': seq,
        't': round(time.time(), 3),
        'mics': [round(level01(x, low_db, high_db), 3) for x in rms],
        'rms': [round(float(x), 5) for x in rms],
        'haptic': [round(result.haptic_left, 3), round(result.haptic_right, 3)],
        'level_bias': round(result.level_bias, 3),
        'direction_deg': None if result.direction_deg is None else round(result.direction_deg, 1),
    }


class SimSender(threading.Thread):
    def __init__(self, url, hz=10.0, device='mic', low_db=-70.0, high_db=-20.0):
        super().__init__(daemon=True)
        self.url, self.period, self.device = url, 1.0 / hz, device
        self.low_db, self.high_db = low_db, high_db
        self._latest = None
        self._event = threading.Event()
        self._stop = threading.Event()
        self.sent = self.failed = 0
        self.last_error = None
        self._seq = 0
        self._next = 0.0

    def offer(self, result):
        """Called from the audio loop; cheap. Keeps only the newest result."""
        now = time.monotonic()
        if now < self._next:
            return
        self._next = now + self.period
        self._seq += 1
        self._latest = payload(result, self._seq, self.device, self.low_db, self.high_db)
        self._event.set()

    def stop(self):
        self._stop.set()
        self._event.set()

    def run(self):
        while not self._stop.is_set():
            self._event.wait(1.0)
            self._event.clear()
            body, self._latest = self._latest, None
            if body is None:
                continue
            data = json.dumps(body).encode('utf-8')
            request = urllib.request.Request(self.url, data=data, method='POST',
                                             headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(request, timeout=0.5) as response:
                    response.read()
                self.sent += 1
                if self.last_error:
                    LOG.info('심 server reachable again: %s', self.url)
                self.last_error = None
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.failed += 1
                message = str(getattr(exc, 'reason', exc))
                if message != self.last_error:
                    LOG.warning('심 send failed (%s): %s', self.url, message)
                self.last_error = message
                self._stop.wait(1.0)  # back off a little while the server is away
