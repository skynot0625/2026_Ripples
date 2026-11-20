# 현재 단계: 공유기 데이터 수신·연결성 점검

사용자 요청에 따라 **공간 누적은 하지 않는다**. 이 실행은 SQLite, reservoir, USB LED를
시작하지 않으며, 입력 원본을 녹화하지 않는다. 기존 Pi/센터 자체 기록 정책은 별도다.
최신 프레임과 상태만 메모리에 두고, `runtime/receiver-status.json` 하나를 덮어쓴다.
이 상태 파일은 수신 횟수·오류·연결 정보이며 센서 시계열 아카이브가 아니다.

```powershell
python -m ripples.receive --seconds 30
# 계속 수신: Ctrl+C로 종료
python -m ripples.receive --seconds 0
# 센터가 다른 PC이면
python -m ripples.receive --center http://192.168.0.2:8000 --seconds 0
```

Windows 가상환경 설치 후 `scripts/receive.cmd`(계속), `scripts/live-check.cmd`(30초)도 사용한다.
기본 경로는 **Pi → 공유기 → 기존 센터 → 심 탑 수신 점검 앱**이다. 심 탑이 TCP9500을
다시 열지 않는다. 인터넷이 없어도 같은 LAN이면 된다. 센터 재연결 복구 수정은 사용자
권한 승인 후 서버를 재시작하여 적용했고 혈 카메라 재연결과 비콘 배치 유지까지 확인했다.

## 주소와 전송 폼

| 송신 / 목적지 | 형식 | 주기·단위 |
|---|---|---|
| 혈 Pi `192.168.0.4` → 센터 `192.168.0.2:9500` | TCP `[u32 LE header 길이][u32 LE payload 길이][JSON][bytes]` | 카메라 약 10Hz, 천장 거리 약 50Hz |
| 맥 Pi `192.168.0.3` → `/api/maek` | HTTP POST JSON, v=1 | 약 10Hz, 3채널 마이크 레벨/RMS |
| 두 Pi → `/api/beacon` | HTTP POST JSON, v=1/type=beacon | 수신기별 약 1Hz, RSSI dBm |
| 센터 → 심 탑 `/ws/monitor` | WS, `next` 요청마다 최신 TOF3 깊이 | 240×180 uint16 LE mm, 약 10Hz |
| 센터 → 심 탑 `/status`, `/inputs`, `/api/beacon` | HTTP GET JSON | 점검 앱은 목표 10Hz로 묶음 조회 |

### 혈

- 연결 시작 `hello`: version=1, tof.width/height/intrinsics/range_mm, lidar.model, rotate.
- `tof`: seq, t_wall_ns, t_mono_ns, width=240, height=180, depth_dtype/conf_dtype=`<u2`.
  payload = depth 전체 86,400B 다음 confidence 전체 86,400B, 총 172,800B.
- `lidar`: seq, t_wall_ns, t_mono_ns, sensor_ms, distance_mm, status, strength, **id=0**, valid.
  위쪽 포인트는 하나다. invalid/status255/0/60m 이상은 거리 없음으로 처리한다.
- Pi 원본은 회전·영역 축약 없이 송신한다. 센터 PC가 confidence/방향을 반영하고,
  심 탑 PC가 3×3 특징을 추출할 수 있다. WS에는 confidence 원본이 들어 있지 않다.
- 현재 센터 threshold는 30, raw 직접 수신의 기본 threshold는 60이므로 경로를 혼용하지 않는다.

### 맥

```json
{"v":1,"device":"mic","seq":1203,"t":1794990142.31,
 "mics":[0.42,0.18,0.37],"rms":[0.0041,0.0013,0.0044],
 "haptic":[0.35,0.30],"level_bias":0.05,"direction_deg":null}
```

- `mics`: L/C/R의 0..1 표시 레벨. band-pass RMS의 −70..−20dBFS를 0..1로 정규화한 값.
- `rms`: digital full-scale 기준 L/C/R RMS. 물리 음압(SPL)이 아니다.
- `haptic`: 좌·우 진동 목표 0..1. `level_bias`: 좌우 상대차 −1..1, 각도가 아니다.
- `direction_deg`: 현재 배치·동기화 미확정으로 null. 임의 방향을 만들지 않는다.
- **현재 Pi는 원음 PCM을 LAN으로 보내지 않는다.** 이 JSON에서 저·중·고음 대역을 복원할 수 없다.
  추후 PCM 확장은 [실시간 규격](realtime.md)의 선택 경로이며 현재 송신 기능으로 간주하지 않는다.

### 비콘

```json
{"v":1,"type":"beacon","receiver":"ripples-tof","seq":812,
 "t_wall_ns":1794990142323320788,"window_s":1.5,
 "nodes":{"2":{"state":"OK","median_dbm":-60,"last_dbm":-61,
                  "n":3,"age_ms":73,"hardware_id":"AC276E80A50A",
                  "reference_rssi_dbm":null}}}
```

- 수신기: 혈 `ripples-tof`, 맥 `mic`. 키 1~5는 Service Data의 group_id다.
- `median_dbm`: 최근 1.5초 중앙값, `n`: 표본 수, `age_ms`: 마지막 BLE 광고 이후 경과 시간.
- 센터 GET 응답은 `receivers`로 묶고 `received_ago_ms`를 추가한다.
- STALE/CONFLICT의 null 값은 정상 결측 표현이다. 0으로 치환하거나 수신 전체를 중단하지 않는다.
- 노드 좌표는 [`live.m30.json`](../configs/live.m30.json)의 5개 확정 XY(m). 원본 mm도 함께 남겼다.
  1m 기준 RSSI는 미보정이다. 상대 RSSI 위치는 대략적인 추정이고 현재 공간 통계에 저장하지 않는다.

## 연결 확인 기준

`receiver-status.json`의 `mode=receive_only`, `accumulation_enabled=false`, `memory_db=null`,
`reservoir_started=false`, `usb_opened=false`를 확인한다. `monitor_received`가 늘고
`center_tof_accepted=true`이면 카메라 수신·폼 검사를 통과한 것이다.
`inputs`에서 각 채널의 source/connected/age_s/rate_hz를 확인한다.
`position_details`의 `insufficient_fresh_nodes`는 센서 수신 자체의 실패가 아니라 위치용 비콘 부족이다.
지금은 일부만 켜둔 상태라 위치는 만들지 않는다. 추후 신선한 비콘 3개 이상과 맥을 켜면
동시 수신 상태를 확인할 수 있다. 일부 기구가 없어도 정상 카메라 데이터는 계속 읽는다.

센터 브리지의 capture_ms는 센터 PC 시간, raw Pi의 순서·간격은 seq/t_mono_ns로 처리한다.
인터넷 없이 틀어진 Pi wall clock 때문에 입력을 버리지 않는다. 원시 스트림 검사 방법과
한계는 [실시간 규격](realtime.md)에 있다. 센터의 테스트 UI는
[시각화 작업실](http://127.0.0.1:8000/?test), [연결 관리](http://127.0.0.1:8000/operator?test)다.
`?test`는 편집 UI 옵션이며 가상 데이터 모드를 뜻하지 않는다.

추후 누적을 요청할 때만 `ripples.live --accumulate`를 사용한다. 그때도 미보정 상대 위치는
estimated DB, RF 보정 후 range_fit은 별도 measured DB로 분리한다.
