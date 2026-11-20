# 감각기구 데이터 흐름

2026년 11월 20일 기준, 갤러리 M30 설치 상태입니다. 혈, 맥, 비콘 수신기가 각자 센서를 읽고 처리한 뒤 공유기를 거쳐 전시컴(Windows, 심 옆)으로 보냅니다. 심 LED 탑을 움직이는 프로그램은 이 레포의 `src/ripples`이고, 탑의 중앙 보드는 USB로 LED2 프레임을 받습니다. 아래는 각 기구가 받는 형식, 처리, 공유기로 보내는 형식, 그리고 심탑이 전시컴에서 읽는 형식과 남은 일입니다.

## 전체 구성

```
 [혈 기구]  Raspberry Pi 4 (192.168.0.4)
   ToF 카메라 240×180 ~30 fps ─┐
   천장 거리 센서 ~50 Hz ──────┼─ collector.py ── 저장(SD) + 실시간 소켓
                               └─ sender.py ──────── TCP 9500 ─────────┐
   BLE 스캐너 (ripples-beacon) ─────────────── HTTP POST /api/beacon ─┤
                                                                      │
 [맥 기구]  Raspberry Pi 3B (192.168.0.3)                             │   공유기 ripplespc
   Teensy 4.0 마이크 3채널 44.1 kHz ── USB 시리얼 ── run.py            │   192.168.0.x
     ├─ 스피커 2개 (I2S, MAX98357A)                                    │
     ├─ 진동모터 2개 (PWM 20 kHz)                                      │
     └─ 레벨 요약 10 Hz ─────────────────────── HTTP POST /api/maek ──┤
   BLE 스캐너 (ripples-beacon) ─────────────── HTTP POST /api/beacon ─┤
                                                                      ▼
 [전시컴]  Windows (192.168.0.2, 유선)  exhibition-server: server.py :8000, :9500
   입력 채널 → 혈·맥 기구 화면, 블라인드 프로젝터, 연결 관리 화면
        │ /ws/monitor (깊이), /status, /inputs, /api/beacon  (docs/realtime.md)
 [심탑 프로그램]  src/ripples  센서 특징 → 공간 통계 → reservoir → RGB565
        └─ USB 시리얼 LED2 프레임 24,980 B ── Nano ESP32 중앙 보드 ── LED 10,440개
```

| 장치 | 주소 | 보내는 것 | 받는 곳 |
|---|---|---|---|
| 혈 Pi | 192.168.0.4 | ToF 깊이 프레임, 천장 거리 | `tcp://192.168.0.2:9500` |
| 혈 Pi | 192.168.0.4 | 비콘 1초 집계 | `http://192.168.0.2:8000/api/beacon` |
| 맥 Pi | 192.168.0.3 | 마이크 레벨과 진동 세기 10 Hz | `http://192.168.0.2:8000/api/maek` |
| 맥 Pi | 192.168.0.3 | 비콘 1초 집계 | `http://192.168.0.2:8000/api/beacon` |
| 심탑 프로그램 | 전시컴 USB | LED2 프레임 | 심 중앙 보드 (COM 포트) |

시각은 장치마다 따로입니다. Pi 시계는 인터넷이 없으면 틀릴 수 있어서, 순서와 간격은 `seq`와 단조 시계(`t_mono_ns`)로 맞추고 실제 시각(`t_wall_ns`, `t`)은 참고로만 씁니다.

## 혈

폴더 `sensors/hyeol-pi` (혈 Pi의 `~/ripples-hyeol-pi`에서 그대로 가져옴, 저장 데이터 제외)

### 센서

| 센서 | 연결 | 주기 | 값 |
|---|---|---|---|
| Arducam ToF 카메라 | CSI | 약 30 fps | 240×180 깊이 mm (`uint16`, 0은 측정 없음), confidence |
| TOFSense 거리 센서 (천장) | UART `/dev/ttyAMA0`, 921600 baud | 약 50 Hz | 거리 mm, status, 신호 세기, 센서 내부 ms |

### 처리 (`collector/collector.py`, 서비스 `ripples-collector`)

센서를 여는 프로그램은 수집단 하나뿐입니다. 수집단은 두 일을 같이 합니다.

1. 저장: `~/ripples-data/<날짜>/`에 ToF 원본(npz, 10초 묶음), ToF 요약 CSV, 거리 CSV(원 프레임 16바이트 포함)를 씁니다. 용량 한도를 넘으면 오래된 ToF 프레임부터 지웁니다. 자세한 형식은 `collector/DATA_FORMAT.md`에 있습니다.
2. 실시간 전달: Unix 소켓 `run/collector.sock`으로 아래 메시지를 내보냅니다. 기구 화면을 그리는 Pi 안의 `app/server.py`와 전시컴으로 보내는 `app/sender.py`가 이 소켓을 읽습니다. 느린 수신자에게는 ToF 최신 한 장만 보내서 센서 쪽은 멈추지 않습니다.

두 센서는 `t_mono_ns`로 맞춥니다. ToF 프레임마다 시각이 가장 가까운 거리 값을 고르면 차이는 최대 약 10 ms입니다.

### 메시지 형식 (`app/collector_protocol.py`)

소켓과 TCP 9500이 같은 형식입니다.

```
[header 길이 u32 LE][payload 길이 u32 LE][header: UTF-8 JSON][payload: 바이너리]
```

| type | header 키 | payload |
|---|---|---|
| `hello` | `version`, `tof: {width, height, intrinsics: {fx, fy, cx, cy}, range_mm}`, `lidar: {model}`, `rotate` | 없음 |
| `tof` | `seq`, `t_wall_ns`, `t_mono_ns`, `width`, `height`, `depth_dtype: "<u2"`, `conf_dtype: "<u2"` | depth (height×width, mm) 바이트 다음에 confidence (같은 크기) 바이트. 240×180이면 172,800 B |
| `lidar` | `seq`, `t_wall_ns`, `t_mono_ns`, `sensor_ms`, `distance_mm`, `status`, `strength`, `id`, `valid` | 없음 |
| `status` | `t_wall_ns`, `tof`, `lidar`, `disk` (1초마다) | 없음 |

`valid`는 status가 255가 아니고 0 < 거리 < 60,000 mm일 때 참입니다. `rotate`는 카메라를 거꾸로 달았을 때 180이며, 원본은 돌리지 않고 받는 쪽이 화면용으로만 돌립니다.

### 공유기로 보내는 것 (`app/sender.py`)

설정 `run/send.json`: `{"host": "192.168.0.2", "port": 9500, "tof_hz": 10, "lidar_hz": 50}` (1초마다 다시 읽음)

* 접속하면 `hello` 한 번, 이후 `tof`를 초당 10장(그 순간 최신 것만), `lidar`를 초당 50개 보냅니다.
* ToF 10 fps 기준 약 1.7 MB/s입니다.
* 끊기면 2초 뒤 다시 붙습니다. 전시컴은 한 번에 한 연결만 받습니다.
* 받는 쪽 예시는 `tools/receive_example.py`입니다.

## 맥

폴더 `sensors/maek-pi` (`run.py`가 맥 Pi의 사용자 서비스 `maek-audio`로 돕니다)

### 센서와 입력 형식

Teensy 4.0이 마이크 3개(왼쪽, 가운데, 오른쪽)를 44,100 Hz, 16비트로 받아 USB 시리얼로 Pi에 보냅니다. 패킷 형식은 `maek_runtime/protocol.py`입니다.

| 부분 | 크기 | 내용 |
|---|---|---|
| header | 24 B | `<8sIIHBBI`: magic `MIC3PCM\n`, seq u32, rate u32 (44100), frames u16 (128), channels u8 (3), bits u8 (16), dropped u32 |
| payload | 768 B | 128 프레임 × 3채널 × int16 LE (채널이 번갈아 놓임) |
| crc | 4 B | header와 payload의 CRC32 |

패킷 하나는 796 B, 초당 약 345개로 약 275 KB/s입니다. Teensy 펌웨어(`mic3_teensy.ino`)는 이 레포에 아직 없습니다.

### 처리

`maek_audio/processor.py`가 대역 통과 후 채널별 RMS를 구합니다. 그 결과로 세 곳에 씁니다.

* 스피커 2개: Pi I2S, MAX98357A 앰프 두 개, ALSA 재생
* 진동모터 2개: PWM 20 kHz, 레벨을 진동 곡선과 감도에 맞춰 0에서 1 사이 세기로 바꿈
* 전시컴 요약: 아래 JSON, 초당 10번

설정은 `config/device.json`(`sim_url`, `sim_hz`, `sim_level_db`, 모터, 버퍼)과 `config/audio.ini`입니다.

### 공유기로 보내는 것 (`maek_runtime/simlink.py`, 형식 `docs/SIM_FORMAT.md`)

`POST http://192.168.0.2:8000/api/maek`, `Content-Type: application/json`, 4 KB 이하. 성공은 204, 형식 오류는 400입니다. 실패한 값은 다시 보내지 않습니다.

```json
{"v": 1, "device": "mic", "seq": 1203, "t": 1794990142.31,
 "mics": [0.42, 0.18, 0.37], "rms": [0.0041, 0.0013, 0.0044],
 "haptic": [0.35, 0.30], "level_bias": 0.05, "direction_deg": null}
```

| 키 | 뜻 |
|---|---|
| `mics` | 마이크 레벨 0에서 1. 대역 통과 RMS를 dBFS로 바꿔 −70 dBFS를 0, −20 dBFS를 1로 둔 값. 음압 보정 아님 |
| `rms` | 위 값의 원래 RMS (디지털 풀스케일 1 기준) |
| `haptic` | 지금 진동모터 목표 세기 [왼쪽, 오른쪽] |
| `level_bias` | 좌우 레벨 차, −1 왼쪽에서 +1 오른쪽. 각도가 아님 |
| `direction_deg` | 소리 방향. 마이크 배치와 동기화가 확인되기 전에는 늘 null |

마이크 원음은 지금 공유기로 보내지 않습니다.

## 비콘

폴더 `sensors/beacon` (혈 Pi와 맥 Pi에 같은 프로그램, 서비스 `ripples-beacon`)

### 송신기

ESP32-C3 비콘 5개(노드 1에서 5)가 BLE 광고 채널 37, 38, 39로 약 100 ms마다 광고합니다. 연결이나 페어링은 없습니다. 노드는 MAC이 아니라 Service Data 안의 번호로 구분합니다.

* Service Data UUID `bf46fcd8-bd8d-5695-a08b-a7c1d419be4e`
* 10바이트 `<BH6sb`: 버전 1, group_id (노드 번호) u16, 장치 ID 6 B, 기준 RSSI int8 (127은 미보정)

### 처리 (`beacon_scanner.py`)

BlueZ와 Bleak로 스캔을 계속 켜 둡니다(`DuplicateData=True`). 1초마다 노드별로 상태를 매깁니다.

| 상태 | 조건 | 내는 값 |
|---|---|---|
| `OK` | 최근 3초 안에 받음 | 마지막 RSSI, 최근 1.5초 중앙값, 개수, age |
| `CONFLICT` | 같은 번호에 다른 장치 ID가 3초 안에 보임 | 값 없음 (두 장치를 섞지 않음) |
| `STALE` | 3초 넘게 못 받음 | 값 없음 (0 dBm으로 채우지 않음) |

원시 수신 기록과 1초 집계는 `~/ripples-data/beacon/<날짜>/`에 CSV로 남깁니다.

### 공유기로 보내는 것

`POST http://192.168.0.2:8000/api/beacon`, 1초마다. 설정은 `run/beacon.json`의 `post_url`, `receiver_id`입니다.

```json
{"v": 1, "type": "beacon", "receiver": "mic", "seq": 21, "t_wall_ns": 1794990142323320788, "window_s": 1.5,
 "nodes": {"1": {"state": "OK", "last_dbm": -78, "median_dbm": -79.0, "n": 2, "age_ms": 461,
                 "hardware_id": "80F1B261750E", "reference_rssi_dbm": null}},
 "nearest": {"node": 3, "zone": null, "median_dbm": -79, "margin_db": 0.0}}
```

`nearest`는 가장 센 노드와 2등과의 차입니다. 거리 값이 아니라 가까운 구역을 고르는 용도입니다. 관리 화면은 1 m 기준 −59 dBm, 감쇠 2.0을 가정해 거리를 어림하며, 현장 보정 전입니다.

## 전시컴

폴더 `exhibition-server` (전시컴 `C:\Users\Admin\tof-web-viewer`의 코드 전체. 가상환경, 저장 데이터, 백업, 파워플랜트 원본 도면은 뺌). 실행: `python exhibition.py start` (`.venv`에 `requirements.txt` 설치 후)

| 입구 | 형식 | 처리 |
|---|---|---|
| TCP 9500 | 혈 메시지 (위 형식) | `collector_client.py`가 읽어 ToF 최신 프레임과 거리 채널을 채움. 5초 무응답 복구 수정을 로컬 파일에 반영하고 사용자 권한 승인 후 서버 재시작까지 완료 |
| `POST /api/maek` | 맥 JSON | 입력 채널 `maek_mic` (= `mics`)와 `maek_features` (방향, 진동, RMS)를 채움. 3초 안에 진짜 값이 오는 동안 가짜 입력은 멈춤 |
| `POST /api/beacon` | 비콘 JSON | 수신기별 마지막 집계를 보관. `GET /api/beacon`이 받은 지 몇 ms인지와 함께 돌려줌 |

받은 값의 지금 상태는 `GET /inputs`, `GET /api/beacon`, 연결 관리 화면 `/operator`에서 봅니다. 전시 실행 설정은 `exhibition.json`(`COLLECTOR_LISTEN=9500`, `--source collector`)입니다.

### 갤러리 3D 시뮬레이터

| 파일 | 내용 |
|---|---|
| `venues/m30/geometry.json` | 갤러리 M30 공간 (형식 `ripples-venue/1`, mm): 바닥과 천장 높이, 벽, 기둥, 유리, 단차, 움직이는 벽과 창의 규격 |
| `venues/m30/layout_presets.json` | 배치 시안 |
| `venues/m30/mockup.json` | 심 구조물 목업 |
| `venues/m30/source/` | 원본 도면 PDF |
| `venue.py`, `simulator_store.py`, `venue3d.html` | 공간 읽기와 배치 검사, 혈과 맥 위치와 배치 상태 저장, 3D 화면 |

지금 배치 상태는 전시컴의 `data\m30\simulator.json`에 저장되고 `GET /api/simulator`의 `layout`으로 읽습니다. 비콘 노드 위치는 그 안의 `layout.beacons`에 `{"1": {"x", "y", "z"}, …}` (mm, 공간 좌표)로 들어갑니다. 공간 전체는 `GET /api/venue`, 화면은 `/venue3d?test`의 「전시 배치」 칸입니다.

전시컴에는 심 LED를 화면으로 미리 보는 시뮬레이터 `/sim`도 있습니다. 이 시뮬레이터는 `uint8 (12, 16, 64, 3)` 프레임(36,864 B)을 `POST /api/sim/frame`으로 받습니다(`sim_feed.py`, `sim_send.py`, 포트 표 `sim_mapping.csv`). 실제 탑의 LED2 형식과 크기가 달라서 탑으로 바로 보낼 수는 없습니다.

## 심탑

심탑 프로그램(`src/ripples`)과 하드웨어 문서(`docs/tower.md`, `docs/sensing.md`)는 이 레포에 먼저 있던 것입니다. 여기서는 감각기구 쪽과 맞닿는 부분만 정리합니다.

### 심탑이 받는 센서 형식 (`src/ripples/sensors.py`)

| 입력 | 심탑이 읽는 형식 | 지금 쓰는 자료 |
|---|---|---|
| 혈 | 혈 메시지와 같은 바이트열을 이어 붙인 `stream.bin`. `tof`에서 회전 보정 후 confidence 60 이상, 0 < depth ≤ 4 m인 화소로 3×3 영역 중앙값을 만들고, `lidar`는 valid와 status를 검사 | 녹화 파일 (`data_led_example/sensors/ripples-hyeol-send-sample_20261116.zip`) |
| 맥 | 1초짜리 WAV (3채널, 44,100 Hz, 16비트) + `metadata.json` (`device_id`, `session_id`, `chunk_id`, `start_frame`, `frames`, `sha256`). 형식은 `data_led_example/sensors/protocols/sound.md` | 합성 음 |
| 비콘 (파일 데모) | 합성 RSSI로 거리·최소제곱 XY | 가상 방 10 m × 10 m, 모서리 4개 |
| 비콘 (기본 live) | 확정 M30 노드 5개와 실제 상대 RSSI로 대략적인 XY | `configs/live.m30.json`. 현재 누적 OFF. 추후 opt-in 시 별도 estimated DB, RF 보정 후 range_fit으로 전환 |

### 심탑이 보내는 LED2 프레임 (`src/ripples/hardware`)

전시컴 USB 시리얼로 중앙 보드(Arduino Nano ESP32)에 보냅니다.

| 부분 | 크기 | 내용 |
|---|---|---|
| header | 20 B | `<4sIHHII`: magic `LED2`, sequence u32, N u16 (65), flags u16 (6), payload_bytes u32 (24,960), payload CRC32 |
| payload | 24,960 B | `[DD 12][MUX 16][가시 슬롯 65]` 순서의 RGB565 (uint16 LE) |

* 합계 24,980 B입니다. 보드는 표시를 마치면 `OK seq rx_us scan_us start_us end_us rx_end_us crc_hex`로 답합니다.
* 프로그램 안의 RGB 배열은 `(12, 16, 65, 3)` uint8이고, `rgb565.InstallationEncoder`가 위 바이트로 바꿉니다. 34개 스트립의 R과 G 교환과 Zone에서 DD로의 매핑은 PC가, 밝기 32는 보드가 맡습니다.
* 각 스트립의 첫 물리 LED는 보드가 검정으로 넣으므로 PC 입력의 슬롯 0은 물리 LED 2번입니다.

### 전시컴에서 심탑으로

심탑 프로그램의 실시간 브리지(`src/ripples/realtime.py`, 설명 `docs/realtime.md`, 기본 설정 `configs/live.m30.json`)가 전시컴을 읽습니다. 혈 Pi는 받는 곳이 하나라서, 심탑은 TCP 9500을 다시 열지 않고 전시컴을 거칩니다. `live.example.json`은 위치를 만들지 않는 수신 진단용입니다.

| 신호 | 심탑이 읽는 곳 | 남은 것 |
|---|---|---|
| 혈 깊이 | `ws /ws/monitor`: 전시컴이 confidence로 거르고 방향을 맞춘 깊이 (`TOF3` 헤더 + uint16). 원시 confidence는 들어 있지 않음 | 심탑 설정의 `monitor_confidence`가 전시컴 `/status.confidence`와 같아야 실측 통계에 넣음 |
| 혈 천장 거리 | `/inputs`의 거리 채널 | 없음 |
| 맥 | `/inputs`의 `maek_mic` (레벨 3개, 0에서 1) | 저음, 중음, 고음 대역은 원음이 있어야 계산됨. 맥 Pi에는 원음 송신이 아직 없음 |
| 비콘 | `/api/beacon`의 수신기별 노드 RSSI | 확정된 `layout.beacons` 5개를 mm→m 변환해 반영 완료. 상대 RSSI 위치 추정 준비. 현재는 수신만 하고 누적하지 않음 |
| 전시컴 수신 | | 복구 수정 파일 백업·반영 및 관리자 권한 서버 재시작 완료. 카메라 재연결·비콘 좌표 유지 확인 |

2026-11-20 마지막 실입력 점검은 비콘 일부만 켠 상태(사용자 확인)였다. 신선한 노드가
3개 미만이면 새 위치/공간 통계를 만들지 않는다. 맥 입력도 없었으므로 동시 기구 실측은
모두 켠 뒤 확인한다. 같은 수신 계약의 합성 HTTP 입력으로 두 기구·누적·입력 소멸을 검증했다.

현재 요청 범위는 [수신 전용 실행과 데이터 폼](RECEIVE_ONLY.md)이다. `python -m ripples.receive`는
DB·reservoir·LED를 시작하지 않는다. 앞으로 표시를 켤 때는 추가 방사형 파동 없는 activity를
기본으로 사용한다. 공간 기억은 별도의 `--accumulate` opt-in이 필요하다.

## 폴더

| 폴더 | 내용 |
|---|---|
| `sensors/hyeol-pi` | 혈 Pi 전체: 수집단, 송신, Pi 안 기구 화면 서버, 설치 스크립트 |
| `sensors/maek-pi` | 맥 Pi: Teensy 수신, 소리 처리, 스피커와 모터, 심 송신 |
| `sensors/beacon` | 비콘 수신기 (오프라인 설치용 wheel 파일은 용량 때문에 빼서, 설치 전에 `wheels/`에 bleak 1.1.1과 dbus-fast를 받아 넣어야 함) |
| `exhibition-server` | 전시컴 서버 전체: 혈 TCP와 맥, 비콘 HTTP 수신, 기구 화면과 블라인드 출력, 갤러리 3D 시뮬레이터(`venues/m30`), 심 LED 시뮬레이터, 연결 관리 화면 |
