"""표시단 쪽 수신기: 수집단(collector/collector.py)의 소켓에서 ToF 프레임 · 천장 거리를 받는다.

server.py 는 --source collector 일 때 `import ArducamDepthCamera as ac` 대신 이 모듈을 쓴다. 그래서 ArducamDepthCamera 와
같은 이름(ArducamCamera, Connection.CSI, FrameType.DEPTH, Control.*, requestFrame/releaseFrame …)을 흉내 낸다.
카메라 처리 코드(camera_loop)는 한 줄도 바뀌지 않는다. --distance collector 는 같은 연결의 거리 값을 "distance" 입력
채널에 넣는다 (tofsense.py 와 같은 값: 미터, 측정 없음은 None).

연결이 끊기면 알아서 다시 붙는다 (collector 를 나중에 켜도, 재시작해도 표시단은 그대로).
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import collector_protocol as proto

SOCK_PATH = Path(__file__).resolve().parent.parent / "run" / "collector.sock"
ROTATE = int(os.environ.get("TOF_ROTATE", "0") or 0)   # 0 또는 180
FPS = float(os.environ.get("TOF_FPS", "0") or 0)   # 화면 쪽이 받는 ToF 수 (0 = 전부). Pi 4 부담 줄임, 저장 원본은 그대로


class _Link:
    """collector 와의 연결 하나 (모듈 전체가 공유)."""

    def __init__(self, path: Path = SOCK_PATH) -> None:
        self.path = path
        self.cond = threading.Condition()
        self.hello: dict | None = None
        self.tof: tuple[int, np.ndarray, np.ndarray] | None = None   # (seq, depth, conf)
        self.connected = False
        self.error: str | None = "collector 연결 전"
        self.lidar_listeners: list = []
        self.last_status: dict | None = None
        self._started = False

    def start(self) -> None:
        with self.cond:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        warned = False
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(self.path))
            except OSError as exc:
                self.error = f"collector 에 연결 안 됨 ({exc.strerror or exc}) · systemctl status ripples-collector"
                if not warned:
                    print(f"collector: {self.error}", flush=True)
                    warned = True
                time.sleep(1.0)
                continue
            print("collector: 연결됨", flush=True)
            warned = False
            with self.cond:
                self.connected, self.error = True, None
            try:
                while True:
                    h, payload = proto.recv(s)
                    self._handle(h, payload)
            except (OSError, ConnectionError, ValueError) as exc:
                print(f"collector: 끊김 ({exc}) · 다시 연결", flush=True)
            finally:
                s.close()
                with self.cond:
                    self.connected, self.error = False, "collector 연결 끊김"
                    self.cond.notify_all()
                for fn in list(self.lidar_listeners):
                    fn(None, "collector 연결 끊김")
                time.sleep(0.5)

    def _handle(self, h: dict, payload: bytes) -> None:
        t = h.get("type")
        if t == "tof":
            w, hh = h["width"], h["height"]
            n = w * hh
            depth = np.frombuffer(payload, dtype=h["depth_dtype"], count=n).reshape(hh, w)
            conf = np.frombuffer(payload, dtype=h["conf_dtype"], count=n, offset=n * 2).reshape(hh, w)
            if ROTATE == 180:   # 카메라를 거꾸로 달았을 때 (settings.sh TOF_ROTATE=180): 화면에 쓰는 그림만 돌림, 저장 원본은 그대로
                depth, conf = depth[::-1, ::-1].copy(), conf[::-1, ::-1].copy()
            with self.cond:
                self.tof = (h["seq"], depth, conf)
                self.cond.notify_all()
        elif t == "lidar":
            m = round(h["distance_mm"] / 1000, 3) if h["valid"] else None
            for fn in list(self.lidar_listeners):
                fn(m, None)
        elif t == "hello":
            with self.cond:
                self.hello = h
                self.cond.notify_all()
        elif t == "status":
            self.last_status = h
            lid = h.get("lidar", {})
            if not lid.get("connected"):
                for fn in list(self.lidar_listeners):
                    fn(None, f"센서: {lid.get('error') or '값 없음'} (collector)")


_link = _Link()


# ---- ArducamDepthCamera 흉내 ---------------------------------------------------------------------------------------
Connection = SimpleNamespace(CSI=0, USB=1)
FrameType = SimpleNamespace(DEPTH=2, RAW=0, CONFIDENCE=1)
Control = SimpleNamespace(RANGE="range", INTRINSIC_FX="fx", INTRINSIC_FY="fy", INTRINSIC_CX="cx", INTRINSIC_CY="cy")


class ArducamCamera:
    def __init__(self) -> None:
        self.last_seq = 0

    def open(self, connection, index=0) -> int:
        _link.start()
        return 0   # 연결 전이어도 성공: requestFrame 이 프레임이 올 때까지 None 을 돌려준다

    def start(self, frame_type=None) -> int:
        return 0

    def stop(self) -> int:
        return 0

    def close(self) -> int:
        return 0

    def setControl(self, control, value) -> int:
        return 0   # 범위 등 카메라 설정은 collector 가 정한다

    def getControl(self, control):
        with _link.cond:
            _link.cond.wait_for(lambda: _link.hello is not None, timeout=5)
            intr = (_link.hello or {}).get("tof", {}).get("intrinsics")
        if not intr or control not in intr:
            raise RuntimeError("collector 가 intrinsics 를 아직 보내지 않음")
        return intr[control] * 100   # SDK 와 같은 단위 (server.py 가 /100)

    def getCameraInfo(self):
        with _link.cond:
            _link.cond.wait_for(lambda: _link.hello is not None, timeout=10)
            tof = (_link.hello or {}).get("tof", {})
        return SimpleNamespace(width=tof.get("width", 240), height=tof.get("height", 180))

    def requestFrame(self, timeout_ms: int = 2000):
        if FPS > 0:   # at most FPS frames a second: wait out the rest of the interval, then take the newest frame
            wait = getattr(self, "next_at", 0) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_at = max(getattr(self, "next_at", 0) + 1 / FPS, time.monotonic() - 0.5 / FPS)
        with _link.cond:
            ok = _link.cond.wait_for(lambda: _link.tof is not None and _link.tof[0] != self.last_seq,
                                     timeout=timeout_ms / 1000)
            if not ok:
                return None
            seq, depth, conf = _link.tof
        self.last_seq = seq
        return SimpleNamespace(depth_data=depth, confidence_data=conf, seq=seq)

    def releaseFrame(self, frame) -> int:
        return 0


# ---- 천장 거리 → 입력 채널 ----------------------------------------------------------------------------------------
def start_distance(channel, stop: threading.Event) -> None:
    channel.source = "TOFSense (collector)"

    def on_value(m, error) -> None:
        if error:
            channel.fail(error)
        else:
            channel.update(m)

    _link.lidar_listeners.append(on_value)
    _link.start()
