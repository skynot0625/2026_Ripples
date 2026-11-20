# 비콘 수신기

혈 Pi와 맥 Pi에 같은 폴더를 올려 BLE 비콘(ESP32-C3, 노드 1~5)을 받는다. 기존 프로그램(혈 ToF·천장 송신, 맥 마이크)과는 따로 도는 서비스(`ripples-beacon`)라 서로 건드리지 않는다. 사양은 `assets/2026-11/RASPBERRY_PI_HANDOFF.md`.

## 들어 있는 것

| 파일 | 내용 |
|---|---|
| `beacon_scanner.py` | 수신 · 해석 · 원시 로그 · 1초 집계 · 전시컴 보내기 |
| `install.sh` | 인터넷 없이 설치(`wheels/`를 `vendor/`에 풂) · 서비스 등록 |
| `wheels/` | Bleak 1.1.1 · dbus-fast(파이썬 3.11/3.12/3.13, aarch64) |
| `beacon.example.json` | 설정 예시 → 설치 때 `run/beacon.json`으로 복사 |

## Pi에 올리기

맥이 Pi와 같은 망에 있을 때, 맥 터미널에서 한 번에 실행한다. 혈 Pi 예시이고, 맥 Pi는 주소만 `mic@mic.local`로 바꾼다.

```bash
scp -r ~/Desktop/mamebin/01_projects/ripples/work/2026-11-18_ble_beacon/beacon ripples-pi:~/ripples-beacon && ssh -t ripples-pi "sudo bash ~/ripples-beacon/install.sh"
```

마지막에 `상태: active`가 나오고, 비콘이 켜져 있으면 30초 뒤 로그에 `우리 비콘 N`이 찍힌다.
- 로그 보기: `journalctl -u ripples-beacon -f`
- 지금 값 보기: `cat ~/ripples-beacon/run/beacon_latest.json`

## 처리 규칙 (사양대로)

- **찾는 기준:** Service Data UUID `bf46fcd8-bd8d-5695-a08b-a7c1d419be4e`의 10바이트만 해석한다(버전 1 · group_id · hardware_id · 기준 RSSI). 이름이나 MAC으로는 찾지 않는다.
- **노드 구분:** 노드 키는 group_id다. 스캐너는 `DuplicateData=True`로 계속 켜 두고, 비콘이 60초 동안 안 들어오면 스캐너를 다시 시작한다.
- **노드 상태:** 1초마다 노드별로 셋 중 하나를 매긴다.
  - `OK`: LAST · 중앙값(최근 1.5초) · 개수 · age(ms)를 낸다.
  - `CONFLICT`: 같은 group에서 다른 hardware_id가 3초 안에 보인 경우다. 값을 내지 않고, 두 장치의 RSSI를 섞지 않는다.
  - `STALE`: 3초 이상 못 받은 경우다. 값을 내지 않으며, 0 dBm으로 채우지 않는다.
- **`nearest`:** OK 노드 중 중앙값이 가장 센 노드와, 2등과의 차(dB)다. 거리가 아니라 「가까운 구역」 판별용이다. `zones`에 노드→구역 이름을 적으면 함께 붙는다.
- **저장:** `~/ripples-data/beacon/<날짜>/`에 두 가지를 쓴다.
  - `beacon_raw_<시>.csv`: 수신 시각 · 수신기 ID · group · hardware_id · RSSI · 기준 RSSI
  - `beacon_1s_<시>.csv`: 1초 집계

## 전시컴으로 보내기 (제안)

맥 Pi가 이미 쓰는 방식(`POST /api/maek`, JSON)에 맞춰, 1초마다 집계 하나를 HTTP로 보낸다. 혈 ToF처럼 큰 데이터가 아니라서 9500 TCP 스트림에 섞지 않는다. 그러면 두 Pi가 같은 방식으로 보내고, 혈 송신이 멈춰도 비콘은 따로 간다.

- 켜기: `run/beacon.json`의 `post_url`에 `http://<전시컴>:8000/api/beacon`을 넣는다. 5초 안에 반영되고 재시작은 필요 없다. 비어 있으면 보내지 않고 저장만 한다.
- 받는 쪽: 현재 `exhibition-server/server.py`에 `POST /api/beacon`과 `GET /api/beacon`이 구현되어 있다. `receiver`별 최신 집계와 수신 후 경과 시간을 심 탑 브리지가 읽는다. 형식과 실시간 경로는 [데이터 흐름](../../docs/DATA_FLOW.md), [심 탑 연동](../../docs/realtime.md)을 따른다.

```json
{"v": 1, "type": "beacon", "receiver": "ripples-tof", "seq": 812, "t_wall_ns": 1794..., "window_s": 1.5,
 "nodes": {
   "1": {"state": "OK", "last_dbm": -61, "median_dbm": -63, "n": 14, "age_ms": 42, "hardware_id": "AABBCCDDEEFF", "reference_rssi_dbm": null},
   "3": {"state": "CONFLICT", "age_ms": 120, "hardware_ids": ["AABB…", "CCDD…"]},
   "4": {"state": "STALE", "age_ms": 5230, "hardware_id": "…"}
 },
 "nearest": {"node": 1, "zone": null, "median_dbm": -63, "margin_db": 8.0}}
```

## 알아 둘 것

- **와이파이와 같은 칩:** Pi 4와 Pi 3B는 와이파이와 블루투스가 한 칩을 나눠 쓴다. 비콘 스캔을 켜면 와이파이 속도가 조금 줄 수 있다. 혈 ToF 송신(약 1.7 MB/s)의 fps를 켜기 전과 비교해 봐야 한다.
- **active 스캔:** 우리 비콘은 스캔 요청에 응답하지 않으므로 결과는 passive와 같다. passive로 바꾸려면 BlueZ 실험 기능이 필요해서 쓰지 않았다.
- **실기기 미확인:** 맥에서는 해석 · 집계 시험(`--selftest`)만 했다. Pi의 BlueZ와 실제 비콘으로는 아직 돌려 보지 않았다.
