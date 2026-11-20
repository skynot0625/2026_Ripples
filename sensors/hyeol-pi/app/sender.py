#!/usr/bin/env python3
"""심 서버로 보내기 (2026-11-11): 수집단(collector) 실시간 소켓의 원본을 그대로 받아, 공유기로 연결된 컴퓨터(전시컴)로
TCP 로 보낸다. 형식 = 수집단과 같은 메시지(app/collector_protocol.py): hello → tof(depth u16 + confidence u16) · lidar.
받는 쪽은 collector_protocol.recv 로 그대로 읽으면 된다 (예: tools/receive_example.py).

설정 run/send.json (1초마다 다시 읽음, 재시작 불필요):
  {"host": "192.168.0.10", "port": 9500, "tof_hz": 10, "lidar_hz": 50}
  host 가 비면 보내지 않음. tof_hz = 초당 보낼 ToF 장 수(최신 것만, 0 = 받는 대로 전부), lidar_hz = 천장 거리 값 수(0 = 전부).
ToF 는 카메라가 달린 방향 그대로(돌리지 않음). hello 에 "rotate"(settings.sh TOF_ROTATE)를 실어 보낸다."""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collector_protocol as proto

ROOT = Path(__file__).resolve().parent.parent
SOCK = ROOT / "run" / "collector.sock"
CONF = ROOT / "run" / "send.json"
DEFAULT = {"host": "", "port": 9500, "tof_hz": 10, "lidar_hz": 50}


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Config:
    def __init__(self) -> None:
        self.mt, self.at, self.v = None, 0.0, dict(DEFAULT)

    def get(self) -> dict:
        if time.monotonic() - self.at > 1.0:
            self.at = time.monotonic()
            try:
                mt = CONF.stat().st_mtime
                if mt != self.mt:
                    self.mt = mt
                    self.v = dict(DEFAULT, **json.loads(CONF.read_text(encoding="utf-8")))
                    log("설정:", self.v)
            except FileNotFoundError:
                self.v = dict(DEFAULT)
            except (OSError, ValueError) as exc:
                log("설정 읽기 실패:", exc)
        return self.v


class Out:
    """the TCP link to the receiver: connects lazily, drops (and retries in 2 s) on any error, never blocks long"""

    def __init__(self) -> None:
        self.sock, self.target, self.retry_at, self.hello = None, None, 0.0, None
        self.sent = {"tof": 0, "lidar": 0}

    def send(self, cfg: dict, data: bytes) -> bool:
        target = (cfg["host"], int(cfg["port"])) if cfg["host"] else None
        if target != self.target:
            self.close(); self.target = target
        if target is None:
            return False
        if self.sock is None:
            if time.monotonic() < self.retry_at:
                return False
            try:
                s = socket.create_connection(target, timeout=2)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1); s.settimeout(1.0)
                if self.hello:
                    s.sendall(self.hello)
                self.sock = s
                log("연결됨:", target)
            except OSError as exc:
                self.retry_at = time.monotonic() + 2.0
                log("연결 안 됨:", target, exc)
                return False
        try:
            self.sock.sendall(data)
            return True
        except OSError as exc:
            log("끊김:", exc); self.close(); self.retry_at = time.monotonic() + 2.0
            return False

    def close(self) -> None:
        if self.sock is not None:
            try: self.sock.close()
            except OSError: pass
        self.sock = None


def main() -> None:
    cfg, out = Config(), Out()
    rotate = int(os.environ.get("TOF_ROTATE", "0") or 0)
    last = {"tof": 0.0, "lidar": 0.0}
    stat_at = time.monotonic()
    while True:
        if not cfg.get()["host"]:   # nowhere to send: do not even read the collector (saves the Pi a core's tenth)
            time.sleep(1.0); continue
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect(str(SOCK))
        except OSError:
            time.sleep(1.0); continue
        log("수집단 연결됨")
        try:
            while True:
                h, payload = proto.recv(s)
                c = cfg.get(); t = h.get("type"); now = time.monotonic()
                if not c["host"]:
                    out.close(); break   # sending turned off: let go of the collector
                if t == "hello":
                    h = dict(h, rotate=rotate); out.hello = proto.pack(h, payload); out.close()
                    continue
                if t not in ("tof", "lidar"):
                    continue
                hz = float(c.get("tof_hz" if t == "tof" else "lidar_hz") or 0)
                if hz > 0 and now < last[t]:   # last = when the next one is due (an even schedule, not "since the last")
                    continue
                if out.send(c, proto.pack(h, payload)):
                    out.sent[t] += 1
                    if hz > 0:
                        last[t] = max(last[t] + 1.0 / hz, now - 0.5 / hz)
                if now - stat_at >= 30 and out.target:
                    log(f"보냄 30초: ToF {out.sent['tof']}장 · 천장 {out.sent['lidar']}개 → {out.target}")
                    out.sent = {"tof": 0, "lidar": 0}; stat_at = now
        except (OSError, ConnectionError, ValueError) as exc:
            log("수집단 끊김:", exc)
        finally:
            s.close()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
