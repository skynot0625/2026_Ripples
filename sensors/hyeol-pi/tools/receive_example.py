#!/usr/bin/env python3
"""심 서버(받는 쪽) 예시: 혈 Pi 의 sender.py 가 보내는 원본을 받아 초당 개수와 마지막 값을 찍는다.
   python receive_example.py [--port 9500]      (collector_protocol.py 가 같은 폴더나 ../app 에 있어야 함)"""
import argparse, socket, sys, time
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "app")]
import numpy as np
import collector_protocol as proto
ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=9500); a = ap.parse_args()
srv = socket.create_server(("0.0.0.0", a.port)); print(f"받는 중: 포트 {a.port}", flush=True)
while True:
    c, addr = srv.accept(); print("연결:", addr, flush=True)
    n, t0, dist = {"tof": 0, "lidar": 0}, time.monotonic(), None
    try:
        while True:
            h, b = proto.recv(c)
            if h["type"] == "hello": print("hello:", h, flush=True); continue
            n[h["type"]] = n.get(h["type"], 0) + 1
            if h["type"] == "lidar": dist = h["distance_mm"]
            if h["type"] == "tof":
                px = h["width"] * h["height"]
                depth = np.frombuffer(b, "<u2", px).reshape(h["height"], h["width"])   # mm, 0 = 측정 없음
                conf = np.frombuffer(b, "<u2", px, px * 2).reshape(h["height"], h["width"])
            if time.monotonic() - t0 >= 2:
                dt = time.monotonic() - t0
                print(f"ToF {n['tof']/dt:.1f}/s · 천장 {n['lidar']/dt:.1f}/s · 천장 {dist} mm", flush=True)
                n, t0 = {"tof": 0, "lidar": 0}, time.monotonic()
    except (OSError, ConnectionError) as exc:
        print("끊김:", exc, flush=True)
