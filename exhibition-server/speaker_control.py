"""Loopback-only, bounded speaker tests. Never accepts commands or paths over HTTP."""
import atexit
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from urllib.parse import urlsplit


def local_request(handler, mutation=False):
    try:
        peer = ipaddress.ip_address(handler.client_address[0])
        host = urlsplit('http://' + handler.headers.get('Host', ''))
        if not peer.is_loopback or host.hostname not in ('localhost', '127.0.0.1', '::1'):
            return False
        if host.port != handler.server.server_port:
            return False
        if mutation:
            origin = urlsplit(handler.headers.get('Origin', ''))
            return origin.scheme == 'http' and origin.netloc == host.netloc
        return True
    except ValueError:
        return False


class SpeakerControl:
    def __init__(self, page_dir):
        self.page_dir = Path(page_dir)
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.process = None
        self.state = 'idle'
        self.speaker = None
        self.message = '실제 출력 대기 · 배선 확인 전'
        atexit.register(self.stop)

    def settings(self):
        path = self.page_dir / 'run' / 'speaker-test.json'
        if path.is_file():
            cfg = json.loads(path.read_text(encoding='utf-8-sig'))
            python = Path(cfg['python'])
            project = Path(cfg['project_dir'])
        else:
            python = Path(sys.executable)
            project = self.page_dir.parent
        config = project / 'configs' / 'audio.realtek.m30.json'
        if not python.is_file() or not config.is_file() or not (project / 'src/ripples/speaker_test.py').is_file():
            raise ValueError('심탑 오디오 실행 경로가 설정되지 않았습니다')
        return python, project, config

    def snapshot(self):
        with self.lock:
            try:
                self.settings()
                configured, reason = True, ''
            except (OSError, ValueError, KeyError) as exc:
                configured, reason = False, str(exc)
            return {'configured': configured, 'reason': reason, 'state': self.state,
                    'speaker': self.speaker, 'message': self.message, 'peak': .02,
                    'token': self.token, 'physical_verified': False}

    def start(self, speaker):
        if type(speaker) is not int or speaker not in (1, 2, 3, 4):
            raise ValueError('speaker는 1~4 정수여야 합니다')
        with self.lock:
            if self.process is not None:
                raise RuntimeError('다른 스피커 시험이 진행 중입니다')
            python, project, config = self.settings()
            env = dict(os.environ, PYTHONPATH=str(project / 'src'))
            command = [str(python), '-X', 'utf8', '-u', '-m', 'ripples.audio_play',
                       '--config', str(config), '--test-speaker', str(speaker)]
            proc = subprocess.Popen(command, cwd=project, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.process, self.state, self.speaker = proc, 'running', speaker
            self.message = f'실제 OUT {speaker} 시험 중 · 디지털 피크 2%'
            threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
            return self.snapshot()

    def _watch(self, proc):
        timed_out = False
        try:
            output, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            output, _ = proc.communicate()
        with self.lock:
            if self.process is not proc:
                return
            self.process = None
            if timed_out or proc.returncode:
                self.state = 'error'
                detail = output.decode('utf-8', errors='replace').strip().splitlines()
                self.message = '출력 오류: ' + ('15초 제한으로 중단' if timed_out else (detail[-1][:240] if detail else '장치 실행 실패'))
            else:
                self.state = 'done'
                self.message = f'OUT {self.speaker} 전송 종료 · 해당 스피커만 들렸는지 직접 확인하세요'

    def stop(self):
        with self.lock:
            proc = self.process
            if proc is not None:
                # Stop only the child owned by this test controller.
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
                self.process = None
            self.state, self.speaker = 'stopped', None
            self.message = '실제 스피커 시험 중지'
            return self.snapshot()


_controllers = {}
_lock = threading.Lock()


def handle(handler, mutation=False):
    if not local_request(handler, mutation):
        handler._json({'error': '실제 출력 시험은 심탑 PC의 localhost 화면에서만 가능합니다'}, 403)
        return
    with _lock:
        controller = _controllers.setdefault(str(handler.page_dir), None)
        if controller is None:
            controller = _controllers[str(handler.page_dir)] = SpeakerControl(handler.page_dir)
    if not mutation:
        handler._json(controller.snapshot())
        return
    if not secrets.compare_digest(handler.headers.get('X-Speaker-Token', ''), controller.token):
        handler._json({'error': '화면을 새로고침하고 다시 시도하세요'}, 403)
        return
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 256 or handler.headers.get('Content-Type') != 'application/json':
            raise ValueError('작은 JSON 요청만 허용합니다')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('JSON object required')
        if body.get('action') == 'stop' and set(body) == {'action'}:
            result = controller.stop()
        elif body.get('action') == 'start' and set(body) == {'action', 'speaker'}:
            result = controller.start(body['speaker'])
        else:
            raise ValueError('지원하지 않는 시험 요청입니다')
        handler._json(result)
    except (ValueError, KeyError, OSError) as exc:
        handler._json({'error': str(exc)}, 400)
    except RuntimeError as exc:
        handler._json({'error': str(exc)}, 409)
