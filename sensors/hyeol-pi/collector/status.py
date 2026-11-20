#!/usr/bin/env python3
"""수집단 상태 한눈에 (check.sh 가 부름): run/collector_status.json 을 읽어 사람이 읽을 줄로."""
import json, sys, time
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "run" / "collector_status.json"
try:
    st = json.loads(p.read_text())
except Exception:
    print("수집단: 상태 파일 없음 · 꺼져 있음? sudo systemctl status ripples-collector"); sys.exit(1)
age = time.time() - st["t_wall_ns"] / 1e9
if age > 5:
    print(f"수집단: {age:.0f}초 전 상태에서 멈춤 · 꺼졌을 수 있음 (sudo systemctl status ripples-collector)")
t, l, d = st["tof"], st["lidar"], st["disk"]
print(f"수집단 ToF: {'정상' if t['connected'] else '안 됨'} · {t['resolution']} · {t['fps']} fps · 누적 {t['frames']}장"
      + (f" · {t['error']}" if t["error"] else ""))
print(f"수집단 천장 거리: {'정상' if l['connected'] else '안 됨'} · {l['distance_m']} m · {l['hz']} Hz · {l['port']}"
      + (f" · {l['error']}" if l["error"] else "") + (f" · 체크섬 오류 {l['bad_checksum']}" if l["bad_checksum"] else ""))
if d["enabled"]:
    print(f"수집단 저장: {d['data_dir']} · {d['used_gb']} GB / 한도 {d['budget_gb']} GB · SD 남은 공간 {d['free_gb']} GB"
          f" · ToF 프레임 {d['tof_save_fps']} fps {'저장 중' if d['saving_frames'] else '저장 멈춤 (공간 부족 또는 0)'}")
else:
    print("수집단 저장: 꺼짐 (RECORD=0)")
print(f"수집단 표시단 연결: {st['clients']}개")
