"""수집단(collector) ↔ 표시단(server.py) 사이의 메시지 형식. 표준 라이브러리만 쓴다.

한 메시지 = [header 길이 u32 LE][payload 길이 u32 LE][header: UTF-8 JSON][payload: 바이너리]

type 별 header (모든 시각은 파이 기준, *_ns 정수):
  hello  {"type","version","tof":{"width","height","intrinsics":{fx,fy,cx,cy}|null,"range_mm"},"lidar":{"model"}}
  tof    {"type","seq","t_wall_ns","t_mono_ns","width","height","depth_dtype":"<u2","conf_dtype":"<u2"}
         payload = depth (height×width, mm, 0=측정 없음) 바이트 + confidence (같은 크기) 바이트
  lidar  {"type","seq","t_wall_ns","t_mono_ns","sensor_ms","distance_mm","status","strength","id","valid"}
  status {"type","t_wall_ns", "tof":{...}, "lidar":{...}, "disk":{...}}   (1초마다)
"""

from __future__ import annotations

import json
import socket
import struct

VERSION = 1
PREFIX = struct.Struct("<II")
MAX_HEADER = 1 << 20
MAX_PAYLOAD = 16 << 20


def pack(header: dict, payload: bytes = b"") -> bytes:
    h = json.dumps(header, separators=(",", ":")).encode()
    return PREFIX.pack(len(h), len(payload)) + h + payload


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("collector closed the connection")
        buf += chunk
    return bytes(buf)


def recv(sock: socket.socket) -> tuple[dict, bytes]:
    hl, pl = PREFIX.unpack(_read_exact(sock, PREFIX.size))
    if hl > MAX_HEADER or pl > MAX_PAYLOAD:
        raise ValueError(f"bad message size {hl}/{pl}")
    header = json.loads(_read_exact(sock, hl))
    payload = _read_exact(sock, pl) if pl else b""
    return header, payload


def lidar_valid(distance_mm: int, status: int) -> bool:
    """tofsense.py 와 같은 기준: status 255 또는 거리 ≤ 0 (또는 60 m 초과) = 측정 없음."""
    return status != 255 and 0 < distance_mm < 60000
