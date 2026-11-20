"""Remote sound settings: a tiny web page on the Pi (default port 8090).

Open http://mic.local:8090 from a laptop on the same Wi-Fi. Changes apply to the
running program without restart (with a short fade) and are saved to
config/live.json so they survive a restart. Standard library only.
"""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import threading

from maek_audio.processor import ProcessorConfig

LOG = logging.getLogger('control')

# name: (label, min, max, step)
TUNING = {
    'output_gain': ('스피커 음량 (1 = 원음 크기, 1보다 크면 키움)', 0.0, 8.0, 0.05),
    'output_peak_limit': ('최대 크기 (이보다 큰 소리는 부드럽게 눌림)', 0.05, 1.0, 0.05),
    'processed_mix': ('대역 강조 비율 (0 = 원음)', 0.0, 1.0, 0.05),
    'band_low_hz': ('강조 대역 아래 (Hz)', 40, 2000, 10),
    'band_high_hz': ('강조 대역 위 (Hz)', 500, 12000, 50),
    'haptic_relative': ('진동 곡선 (0 = 소리 크기 그대로, 1 = 먼 소리·가까운 소리 비슷하게)', 0.0, 1.0, 0.1),
    'haptic_margin_db': ('[곡선 1] 주변보다 몇 dB 커야 진동 시작', 0.0, 20.0, 1.0),
    'haptic_range_db': ('[곡선 1] 시작에서 몇 dB 더 크면 최대 (작을수록 더 비슷하게)', 2.0, 40.0, 1.0),
    'haptic_floor_rms': ('[곡선 0] 진동이 시작되는 마이크 레벨', 0.0005, 0.05, 0.0005),
    'haptic_full_rms': ('[곡선 0] 진동이 최대가 되는 마이크 레벨', 0.005, 0.3, 0.005),
}
# Not part of the DSP config: used by the motor output in run.py.
MOTOR = {
    'motor_max_duty': ('모터 최대 세기 (%)', 0, 100, 5),
    'motor_min_duty': ('모터 시작 세기 (%) — 모터가 실제로 도는 가장 낮은 값', 0, 80, 5),
}
MOTOR_DEFAULTS = {'motor_max_duty': 80.0, 'motor_min_duty': 25.0}
MICS = ('MIC1 · 핀 8', 'MIC2 · 핀 6', 'MIC3 · 핀 9')


class LiveControl:
    """Thread-safe holder of live values; the audio loop polls `version`."""

    def __init__(self, settings, mic_gains, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.base = settings
        self.values = {name: getattr(settings.processor, name) for name in TUNING}
        self.values.update(MOTOR_DEFAULTS)
        self.mic_gains = [float(g) for g in mic_gains]
        self.base_gains = list(self.mic_gains)
        self.status = {}
        self.version = 0
        if self.path.is_file():
            try:
                saved = json.loads(self.path.read_text())
                self._validate(saved)
                self.values.update({k: float(v) for k, v in saved.items() if k in TUNING or k in MOTOR})
                if 'mic_gains' in saved:
                    self.mic_gains = [float(g) for g in saved['mic_gains']]
                LOG.info('Loaded remote settings from %s', self.path)
            except (ValueError, OSError) as exc:
                LOG.warning('Ignoring %s: %s', self.path, exc)

    def settings(self):
        with self.lock:
            processor = replace(self.base.processor, **{k: v for k, v in self.values.items() if k in TUNING})
            return replace(self.base, processor=processor), list(self.mic_gains)

    def _validate(self, data):
        values = dict(self.values)
        for key, value in data.items():
            if key in TUNING:
                values[key] = float(value)
            elif key in MOTOR:
                values[key] = float(value)
                if not MOTOR[key][1] <= values[key] <= MOTOR[key][2]:
                    raise ValueError('%s must be %s..%s' % (key, MOTOR[key][1], MOTOR[key][2]))
            elif key == 'mic_gains':
                gains = [float(g) for g in value]
                if len(gains) != 3 or any(not 0 <= g <= 2 for g in gains):
                    raise ValueError('mic_gains needs three values in 0..2')
            else:
                raise ValueError('unknown setting %r' % key)
        if not 0 <= values['output_gain'] <= 8:
            raise ValueError('스피커 음량은 0~8 사이')
        if values['motor_min_duty'] > values['motor_max_duty']:
            raise ValueError('motor start strength must not exceed max strength')
        replace(self.base.processor, **{k: v for k, v in values.items() if k in TUNING})  # DSP checks
        return values

    def update(self, data):
        with self.lock:
            values = self._validate(data)
            self.values = values
            if 'mic_gains' in data:
                self.mic_gains = [float(g) for g in data['mic_gains']]
            self.version += 1
            snapshot = dict(self.values, mic_gains=self.mic_gains)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(snapshot, indent=2) + '\n')
        tmp.replace(self.path)
        LOG.info('Remote settings changed: %s', snapshot)

    def reset(self):
        with self.lock:
            self.values = {name: getattr(self.base.processor, name) for name in TUNING}
            self.values.update(MOTOR_DEFAULTS)
            self.mic_gains = list(self.base_gains)
            self.version += 1
        if self.path.exists():
            self.path.unlink()

    def motor(self):
        with self.lock:
            return self.values['motor_min_duty'], self.values['motor_max_duty']

    def state(self):
        with self.lock:
            return {'values': dict(self.values), 'mic_gains': list(self.mic_gains),
                    'status': dict(self.status),
                    'fields': {k: {'label': v[0], 'min': v[1], 'max': v[2], 'step': v[3]}
                               for k, v in list(TUNING.items()) + list(MOTOR.items())},
                    'mics': list(MICS)}


PAGE = (Path(__file__).with_name('control.html')).read_text(encoding='utf-8')


def serve(control, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, kind='application/json; charset=utf-8'):
            data = body.encode('utf-8') if isinstance(body, str) else body
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ('/', '/index.html'):
                return self._send(200, PAGE, 'text/html; charset=utf-8')
            if self.path == '/api/state':
                return self._send(200, json.dumps(control.state()))
            self._send(404, '{"error":"not found"}')

        def do_POST(self):
            try:
                length = int(self.headers.get('Content-Length') or 0)
                data = json.loads(self.rfile.read(min(length, 10000)) or b'{}')
                if self.path == '/api/set':
                    control.update(data)
                elif self.path == '/api/reset':
                    control.reset()
                else:
                    return self._send(404, '{"error":"not found"}')
                self._send(200, json.dumps(control.state()))
            except (ValueError, TypeError) as exc:
                self._send(400, json.dumps({'error': str(exc)}))

    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    LOG.info('Remote settings page: http://mic.local:%d (or this Pi\'s IP)', port)
    return server
