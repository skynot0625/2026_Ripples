#!/usr/bin/env python3
"""저장된 데이터 읽는 예시 (팀원용). 노트북에서도 numpy 만 있으면 됨.

    python read_example.py ~/ripples-data/2026-11-10

ToF 프레임과 천장 거리를 t_mono_ns(파이의 같은 시계)로 맞추는 방법까지 보여준다."""
import csv, gzip, sys
from pathlib import Path

import numpy as np

day = Path(sys.argv[1]).expanduser()

# 1) 천장 거리: 50 Hz 전부
rows = []
for f in sorted(day.glob("lidar_*.csv*")):
    with (gzip.open(f, "rt") if f.suffix == ".gz" else open(f)) as fh:
        rows += list(csv.DictReader(fh))
t_lidar = np.array([int(r["t_mono_ns"]) for r in rows])
d_lidar = np.array([int(r["distance_mm"]) if r["valid"] == "1" else -1 for r in rows])
print(f"천장 거리 {len(rows)}개 · 유효 {np.mean(d_lidar > 0) * 100:.1f}%")

# 2) ToF 프레임: npz 하나 = 10초 묶음 (~300장)
for f in sorted((day / "tof_frames").glob("tof_*.npz"))[:3]:
    z = np.load(f)
    print(f"{f.name}: {z['depth'].shape[0]}장 · depth {z['depth'].shape} {z['depth'].dtype} · conf {z['conf'].dtype}")
    for k in range(min(2, len(z["seq"]))):
        depth, conf = z["depth"][k], z["conf"][k]
        valid = (depth > 0) & (conf >= 30)
        # 3) 같은 시각의 천장 거리: t_mono_ns 가 가장 가까운 것 (50 Hz 라 최대 약 10 ms 차이)
        j = np.searchsorted(t_lidar, z["t_mono_ns"][k]).clip(1, len(t_lidar) - 1) if len(t_lidar) else None
        near = None
        if j is not None:
            j = j - 1 if abs(t_lidar[j - 1] - z["t_mono_ns"][k]) < abs(t_lidar[j] - z["t_mono_ns"][k]) else j
            near = (d_lidar[j], (t_lidar[j] - z["t_mono_ns"][k]) / 1e6)
        print(f"  frame seq {z['seq'][k]}: 유효 {valid.mean() * 100:.0f}% · 중앙값 {np.median(depth[valid]) if valid.any() else '-'} mm"
              + (f" · 천장 {near[0]} mm (시간차 {near[1]:+.1f} ms)" if near else ""))
