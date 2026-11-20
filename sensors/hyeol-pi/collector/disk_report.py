#!/usr/bin/env python3
"""저장 용량 실측 → 하루 · 한도 기준 예측.  ~/tof-env/bin/python collector/disk_report.py [DATA_DIR]

최근 1시간(또는 있는 만큼)의 파일 크기를 종류별로 재서 시간당 증가량을 계산하고, DATA_BUDGET_GB 안에 며칠치가
들어가는지 보여준다. 실제 센서 데이터로 재야 정확하다 (깊이맵의 압축률은 장면에 따라 달라서)."""
import re, shutil, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collector import load_settings, ROOT  # noqa: E402

cfg = load_settings(ROOT / "settings.sh")
root = Path(sys.argv[1] if len(sys.argv) > 1 else cfg["DATA_DIR"]).expanduser()
files = [p for p in root.rglob("*") if p.is_file()]
if not files:
    print(f"{root} 에 파일이 없음"); sys.exit(1)
now = time.time()
window = 3600
recent = [p for p in files if now - p.stat().st_mtime < window]
span = min(window, now - min(p.stat().st_mtime for p in recent)) if recent else 0
kinds = {"ToF 프레임 (npz)": r"tof_\d.*\.npz$", "ToF 요약 CSV": r"tof_summary", "천장 거리 CSV": r"lidar_"}
print(f"저장 폴더 {root} · 전체 {sum(p.stat().st_size for p in files) / 1e9:.2f} GB · 파일 {len(files)}개")
print(f"SD 카드: 전체 {shutil.disk_usage(root).total / 1e9:.1f} GB · 남음 {shutil.disk_usage(root).free / 1e9:.1f} GB")
if span < 120:
    print("최근 데이터가 2분 미만이라 예측이 부정확함 · 몇 분 더 모은 뒤 다시"); sys.exit(0)
total_rate = 0.0
for name, pat in kinds.items():
    size = sum(p.stat().st_size for p in recent if re.search(pat, p.name))
    rate = size / span * 3600
    total_rate += rate
    print(f"  {name:14s} 시간당 {rate / 1e6:8.1f} MB · 하루(24h) {rate * 24 / 1e9:6.2f} GB")
budget = float(cfg["DATA_BUDGET_GB"]) * 1e9
print(f"  {'합계':14s} 시간당 {total_rate / 1e6:8.1f} MB · 하루(24h) {total_rate * 24 / 1e9:6.2f} GB")
if total_rate > 0:
    print(f"한도 {budget / 1e9:.0f} GB 면 약 {budget / total_rate:.0f}시간치 ({budget / total_rate / 24:.1f}일) 를 보관하고, 그보다 오래된 것부터 지워짐")
