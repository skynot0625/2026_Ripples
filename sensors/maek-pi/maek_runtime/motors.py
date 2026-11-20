"""Vibration motor output: haptic strength (0..1) -> Pi hardware PWM duty.

Hardware (see ~/pi-vibration-test/README.md on the Pi): DRV8833, motor A on
GPIO12 (PWM0), motor B on GPIO13 (PWM1), dtoverlay=pwm-2chan,pin=12,func=4,
pin2=13,func2=4. The user must be in the gpio group (no sudo needed).
Shares the lock file with pi-vibration-test so both cannot drive at once.
"""
import fcntl
import logging
import os
import time
from pathlib import Path

LOG = logging.getLogger('motors')
LOCK = '/run/lock/pi-vibration-test.lock'


def _find_chip(root=Path('/sys/class/pwm')):
    for path in sorted(root.glob('pwmchip*')):
        compatible = path / 'device/of_node/compatible'
        if compatible.exists() and b'brcm,bcm2835-pwm' in compatible.read_bytes():
            if int((path / 'npwm').read_text()) >= 2:
                return path
    raise RuntimeError('Pi PWM not found (dtoverlay=pwm-2chan on GPIO12/13 needed)')


class PwmMotors:
    def __init__(self, frequency=20000, swap=False):
        self.period = round(1_000_000_000 / frequency)
        self.swap = swap
        self.channels = []
        self.lock = None
        self.last = None

    def open(self):
        self.lock = open(LOCK, 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RuntimeError('another motor program is running')
        chip = _find_chip()
        for index in (0, 1):
            path = chip / ('pwm%d' % index)
            if not path.exists():
                (chip / 'export').write_text(str(index))
            # udev fixes permissions a moment after export
            deadline = time.monotonic() + 3
            while not os.access(path / 'duty_cycle', os.W_OK):
                if time.monotonic() > deadline:
                    raise RuntimeError('no permission for %s (is the user in the gpio group?)' % path)
                time.sleep(0.05)
            self.channels.append(path)
            (path / 'duty_cycle').write_text('0')
            (path / 'period').write_text(str(self.period))
            (path / 'enable').write_text('1')
        LOG.info('Motors ready on %s (%d Hz)', chip, round(1e9 / self.period))
        return self

    def set(self, left_pct, right_pct):
        values = (right_pct, left_pct) if self.swap else (left_pct, right_pct)
        duties = tuple(round(self.period * max(0.0, min(100.0, v)) / 100) for v in values)
        if duties == self.last:
            return
        for path, duty in zip(self.channels, duties):
            (path / 'duty_cycle').write_text(str(duty))
        self.last = duties

    def close(self):
        for path in self.channels:
            try:
                (path / 'duty_cycle').write_text('0')
            except OSError as exc:
                LOG.error('Could not stop motor %s: %s', path, exc)
        self.channels = []
        if self.lock is not None:
            self.lock.close()
            self.lock = None


def duty(strength, min_duty, max_duty, deadzone=0.03):
    """Haptic strength 0..1 -> duty %. Below the deadzone the motor is off;
    above it, start at min_duty so the motor actually spins."""
    if strength < deadzone:
        return 0.0
    return min_duty + (max_duty - min_duty) * min(1.0, strength)
