#!/usr/bin/env python3
"""혈 Pi 짧은 녹화 (2026-11-17): 화면 · 송신을 끄지 않고, 수집단 소켓(run/collector.sock)에서 원본을 N초만 받아 저장한다.
   python3 app/record_clip.py [초=30] [--wait 5] [--name 이름]
저장: ~/ripples-data/clips/<날짜_시각>[_이름]/ — raw 와 같은 모양(tof_frames/*.npz · lidar.csv · tof_camera.json)이라
replay_window.py · make_send_sample.py 에 그대로 넣을 수 있다. 끝나면 폴더와 용량을 찍는다. (30초 ≈ 100 MB)"""
import argparse, csv, json, os, socket, sys, time
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "app")]
import collector_protocol as proto
ap = argparse.ArgumentParser(); ap.add_argument("seconds", nargs="?", type=float, default=30)
ap.add_argument("--wait", type=float, default=5, help="시작 전 기다림(자리 잡기)"); ap.add_argument("--name", default="")
ap.add_argument("--sock", default=str(HERE.parent / "run" / "collector.sock")); ap.add_argument("--out", default=os.path.expanduser("~/ripples-data/clips"))
a = ap.parse_args()
out = Path(a.out) / (time.strftime("%Y%m%d_%H%M%S") + (f"_{a.name}" if a.name else "")); (out / "tof_frames").mkdir(parents=True)
for i in range(int(a.wait), 0, -1): print(f"{i}…", flush=True); time.sleep(1)
print(f"녹화 시작 · {a.seconds:.0f}초", flush=True)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect(a.sock)
lid = open(out / "lidar.csv", "w", newline=""); lw = csv.writer(lid)
lw.writerow(["seq", "t_wall_ns", "t_mono_ns", "sensor_ms", "distance_mm", "status", "strength", "valid"])
buf = {k: [] for k in ("seq", "t_wall_ns", "t_mono_ns", "depth", "conf")}; chunk_t = None; n = {"tof": 0, "lidar": 0}
def flush():
    global chunk_t
    if buf["seq"]:
        name = time.strftime("tof_%Y%m%d_%H%M%S", time.localtime(buf["t_wall_ns"][0] / 1e9))
        np.savez_compressed(out / "tof_frames" / f"{name}.npz", **{k: np.array(v) for k, v in buf.items()})
        for v in buf.values(): v.clear()
    chunk_t = None
end = time.monotonic() + a.seconds; last_print = 0
while time.monotonic() < end:
    h, b = proto.recv(s); t = h.get("type")
    if t == "hello":
        (out / "tof_camera.json").write_text(json.dumps({"tof": h.get("tof")}, ensure_ascii=False, indent=1)); continue
    if t == "lidar":
        lw.writerow([h["seq"], h["t_wall_ns"], h["t_mono_ns"], h.get("sensor_ms", 0), h["distance_mm"], h.get("status", 0), h.get("strength", 0), int(bool(h.get("valid")))]); n["lidar"] += 1
    elif t == "tof":
        px = h["width"] * h["height"]
        buf["depth"].append(np.frombuffer(b, "<u2", px).reshape(h["height"], h["width"]))
        buf["conf"].append(np.clip(np.frombuffer(b, "<u2", px, px * 2), 0, 255).astype(np.uint8).reshape(h["height"], h["width"]))
        for k in ("seq", "t_wall_ns", "t_mono_ns"): buf[k].append(h[k])
        n["tof"] += 1; chunk_t = chunk_t or time.monotonic()
        if time.monotonic() - chunk_t >= 10: flush()
    left = end - time.monotonic()
    if int(left) // 5 != last_print and int(left) % 5 == 0: last_print = int(left) // 5; print(f"남은 {int(left)}초 · ToF {n['tof']}장", flush=True)
flush(); lid.close(); s.close()
size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
print(f"끝 · ToF {n['tof']}장 ({n['tof']/a.seconds:.1f} fps) · 천장 {n['lidar']}개 · {size/1e6:.0f} MB\n→ {out}", flush=True)
