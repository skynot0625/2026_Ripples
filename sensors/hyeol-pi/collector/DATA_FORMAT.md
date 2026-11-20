# 수집 데이터 형식 (collector v1)

수집단(`collector/collector.py`, 서비스 `ripples-collector`)이 센서를 혼자 읽고 **파일 저장**과 **실시간 전달**을 같이 한다.
표시단(`app/server.py --source collector`)이나 외부 전송은 센서를 직접 열지 않고 아래 둘 중 하나로 받는다.

## 1. 센서별 주기와 내용

| 스트림 | 주기 (실측) | 내용 |
|---|---|---|
| ToF 깊이 | 카메라 fps (Arducam 240×180, 보통 ~30 fps) | depth (mm, `uint16`, 0 = 측정 없음) + confidence |
| 천장 거리 | ~50 Hz (TOFSense, 921600 baud) | distance_mm, status, strength, 센서 내부 시간 |
| 상태 | 1 Hz | 스트림별 실측 Hz, 마지막 값 이후 시간, 오류, 디스크 |

모든 메시지·행에 `seq`(스트림별 일련번호, 빠지면 손실), `t_mono_ns`(파이 단조 시계: **센서끼리 맞출 때 기준**),
`t_wall_ns`(실제 시각, Pi 시계가 맞을 때만 의미 있음)가 붙는다.
두 센서 맞추기: ToF 프레임마다 `t_mono_ns`가 가장 가까운 거리 값 (50 Hz라 최대 약 10 ms 차이). `read_example.py` 참고.

## 2. 파일 (`DATA_DIR`, 기본 `~/ripples-data`)

```
ripples-data/2026-11-10/
  session_HHMMSS.json            수집단이 켜질 때마다: 설정, 호스트, 형식 버전
  tof_camera.json                해상도, intrinsics (fx fy cx cy), 범위
  lidar_20261110_14.csv.gz       천장 거리, 50 Hz 전부, 1시간 단위 (쓰는 중엔 .csv, 시간이 바뀌면 gzip)
  tof_summary_20261110_14.csv.gz ToF 프레임마다 요약 통계, 전체 fps, 1시간 단위
  tof_frames/tof_20261110_140210.npz  ToF raw 깊이맵 + confidence, 들어온 프레임 전부(~30 fps), 10초 단위 묶음
```

- `lidar_*.csv`: `seq,t_wall_ns,t_mono_ns,sensor_ms,distance_mm,status,strength,valid,raw_hex`
  (valid=0 이면 측정 없음, raw_hex = 센서가 보낸 16바이트 원 프레임 그대로 — 50 Hz 전부)
- `tof_summary_*.csv`: `seq,t_wall_ns,t_mono_ns,valid_ratio,center_mm,median_mm,p05_mm,p95_mm`
  (유효 = depth>0 이고 confidence≥30, center = 가운데 5×5 중앙값)
- `tof_*.npz` (`np.load`): `seq (N,)`, `t_wall_ns (N,)`, `t_mono_ns (N,)`, `depth (N,180,240) uint16 mm`,
  `conf (N,180,240) uint8` (255에서 잘림)

## 3. 실시간 (`run/collector.sock`, Unix 소켓)

메시지 = `[header 길이 u32 LE][payload 길이 u32 LE][header JSON][payload]`, 형식은 `app/collector_protocol.py`.
접속하면 `hello`(해상도·intrinsics) → 이후 `tof`(payload = depth `<u2` + conf `<u2`), `lidar`, `status`.
느린 수신자에게는 ToF 최신 1장만 보낸다 (센서 쪽은 절대 안 멈춤). 예시 수신기: `app/collector_client.py`.

## 4. 용량 (실측은 `collector/disk_report.py`)

| 항목 | 원본 | 저장 방식 | 대략 |
|---|---|---|---|
| ToF raw 30 fps (기본) | 129.6 KB/장 → 3.9 MB/s (14 GB/시간) | npz deflate 1 | ~6–9 GB/시간 (장면에 따라) |
| ToF 1 fps (`TOF_SAVE_FPS=1`) | 129.6 KB/s | npz | ~200–350 MB/시간 |
| ToF 요약 30 fps | | csv.gz | ~2–3 MB/시간 |
| 천장 거리 50 Hz + raw | 800 B/s (원 프레임) | csv.gz | ~5–8 MB/시간 |

`DATA_BUDGET_GB`(기본 14 → SD 에 약 2시간치가 굴러가며 남음)를 넘으면 오래된 ToF 프레임부터 지우고, SD 남은 공간이 `MIN_FREE_GB`(기본 3) 아래면
ToF 프레임 저장만 멈춘다 (CSV는 작아서 계속).

Pi 쓰기 속도가 못 따라가면 (대기 300장 초과) ToF 프레임만 버리고 개수를 status 의 `tof_dropped` 에 남긴다.
오래 보관할 데이터는 맥으로 가져온다: `bash pull_data.sh` (맥에서).
