import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import time
import wave


def detect_device(cards_text=None):
    if cards_text is None:
        cards_text = Path('/proc/asound/cards').read_text()
    matches = []
    for line in cards_text.splitlines():
        match = re.match(r'\s*(\d+)\s+\[([^]]+)\]', line)
        if match and ('max98357' in line.lower() or 'sndrpimax' in line.lower()):
            matches.append(match.group(2).strip())
    if len(matches) != 1:
        raise RuntimeError('MAX98357A 사운드카드를 하나로 찾지 못했습니다. setup_pi.sh와 재부팅 후 --list-devices를 확인하세요.')
    return 'plughw:CARD=%s,DEV=0' % matches[0]

class WavSink:
    def __init__(self, path):
        # Exclusive creation: never overwrite a previous recording.
        self.file = open(path, 'xb')
        self.wav = wave.open(self.file, 'wb')
        self.wav.setnchannels(2)
        self.wav.setsampwidth(2)
        self.wav.setframerate(44100)
    def write(self, data):
        self.wav.writeframesraw(data)
    def close(self):
        self.wav.close()
        self.file.close()

class AlsaSink:
    def __init__(self, device, stop, buffer_ms=40, period_ms=10):
        if not shutil.which('aplay'):
            raise RuntimeError('aplay가 없습니다. bash setup_pi.sh를 먼저 실행하세요.')
        self.stop = stop
        self.process = subprocess.Popen(
            ['aplay', '-q', '-D', device, '-t', 'raw', '-f', 'S16_LE', '-r', '44100',
             '-c', '2', '--buffer-time=%d' % (buffer_ms * 1000), '--period-time=%d' % (period_ms * 1000)],
            stdin=subprocess.PIPE, bufsize=0)
        os.set_blocking(self.process.stdin.fileno(), False)
        # Keep the pipe tiny so writes are paced by the sound card. The default
        # 64 KiB pipe silently adds ~370 ms of latency and hides clock drift.
        try:
            import fcntl
            fcntl.fcntl(self.process.stdin.fileno(), getattr(fcntl, 'F_SETPIPE_SZ', 1031), 4096)
        except (ImportError, OSError):
            pass
    def write(self, data):
        view = memoryview(data)
        deadline = time.monotonic() + 2
        fd = self.process.stdin.fileno()
        while view and not self.stop.is_set():
            if self.process.poll() is not None:
                raise RuntimeError('aplay 종료: 위 ALSA 오류/배선/장치 설정을 확인하세요.')
            if time.monotonic() > deadline:
                raise RuntimeError('오디오 출력이 2초 이상 정지했습니다.')
            _, writable, _ = select.select([], [fd], [], 0.1)
            if writable:
                try:
                    count = os.write(fd, view)
                    view = view[count:]
                except BlockingIOError:
                    pass
    def close(self):
        # Terminate rather than drain stale audio after Ctrl+C.
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
