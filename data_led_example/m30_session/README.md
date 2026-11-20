# M30 Activate / Sleep — 명시적인 예시 데이터 폼

**전시장 배치와 비콘 좌표는 실제 M30 시뮬레이션 설정**, 기구의 이동·라이다·음량·RSSI는
고정 seed로 생성한 **합성 예시**다. `example:true`를 모든 정규화 이벤트에 넣었다.
현장 수신이나 사용자 DB에 이 데이터를 주입하지 않는다.

결과: [전시장 + Activate + Sleep GIF](../../docs/media/m30_active_sleep.gif).

## 파일 구성

| 파일 | 내용 |
|---|---|
| [venue.json](venue.json) | 센터 `/api/venue`의 M30 평면·벽·기둥·가구 + `/api/simulator`의 현재 배치 스냅샷 |
| [event.schema.json](event.schema.json) | 정규화 이벤트의 JSON Schema 2020-12 |
| [sample_hyeol.json](sample_hyeol.json) / [sample_maek.json](sample_maek.json) | 각 기구의 완전한 이벤트 1개 |
| [events.jsonl](events.jsonl) | 두 기구 각각 10Hz × 240초 = 4,800개. 한 줄에 JSON 하나 |
| [beacon_wire.jsonl](beacon_wire.jsonl) | 기존 POST `/api/beacon` 형태의 합성 메시지 480개(두 수신기 × 1Hz) |
| [maek_wire.jsonl](maek_wire.jsonl) | 기존 POST `/api/maek` 형태의 합성 메시지 2,400개 |
| [hyeol_sample.bin](hyeol_sample.bin) | 기존 TCP 형식의 hello + 원시 ToF 1장 + 위쪽 거리 1개 |
| [route.json](route.json) | M30 공간 좌표(m)의 닫힌 이동 경로 |
| [manifest.json](manifest.json) | 예시 여부·seed·단위·파일별 크기/SHA256 |

이 폴더는 약 3MB이며 원시 ToF를 240초 전부 보관하지 않는다. 카메라 원시 예시는 한 프레임이다.
`wire` 자료는 **네트워크 형식의 예시 파일**이며 생성/재생 명령은 실제 센터에 POST하지 않는다.

## 공통 정규화 이벤트

Pi 통신 규약을 바꾸는 것이 아니라 **심 탑에서 해석한 뒤 재생·검증하기 위한 예시 폼**이다.
Activate/Sleep은 표시 상태이므로 같은 이벤트를 사용하며 센서 패킷에 상태를 넣지 않는다.

| 필드 | 형·단위 | 규칙 |
|---|---|---|
| schema | 문자열 | `ripples.example-event/1` 고정 |
| example | boolean | 반드시 true. 실측으로 오인해 가져오지 않음 |
| session_id | 문자열 | 예시 세션 ID. 기구 둘은 같은 세션 사용 |
| source | enum | `hyeol` / `maek` |
| seq | 정수 | 기구별 증가, 중복 불가. 현재 0..2399 |
| t_ms | 정수, ms | 공유된 **예시 세션 시작 이후** 시간. 현재 0..239900 |
| position.frame | 문자열 | `m30`: X3·Y7 원점, +X=X3→X5, +Y=Y7→Y3 |
| position.method | 문자열 | `simulation_truth`: 예시에서 알고 있는 위치. 실측 추정이라는 뜻이 아님 |
| position.xy_m | `[x,y]`, m | LED 맵핑에 사용할 알려진 시뮬레이터 위치 |
| position.relative_xy_m | `[x,y]`, m | 동일 예시 RSSI로 계산한 상대 가중 위치, 지도에서 빈 원으로만 비교 |
| position.beacon_seq | 정수 | 1Hz 비콘 메시지 번호 |
| position.beacon_age_ms | 정수, ms | 비콘 이후 경과. `beacon_seq*1000+age=t_ms`, 3초 미만 |
| measurement | 객체 | 아래 기구별 폼. 이름 없는 임의 feature를 추가하지 않음 |

Pi wall clock은 예시 세션 시간과 다르다. wire 메시지의 t_wall_ns/t는 고정된 예시 날짜의
시각이고, 실제 운전은 기존 seq/monotonic 수신 규칙을 사용한다. 프리뷰에서는 t_ms로 정렬한다.
단위가 다른 mm 값을 `xy_m`이나 `depth_m`에 그대로 넣으면 안 된다.

### 혈 measurement

```json
{"depth_m":[null,null,null,null,null,null,2.961,2.936,2.922],
 "coverage":[0,0,0,0,0,0,0.9192,0.8223,0.8888],"upward_m":2.2}
```

- 카메라를 회전 보정한 뒤 **3×3 row-major(위왼쪽→위오른쪽→다음 행)** 배열이다.
- depth_m은 유효 화소의 거리 중앙값, 0보다 크고 4m 이하. 관측 없음은 **null**이다.
- coverage는 같은 영역의 유효 화소 비율 0..1. null depth의 coverage는 반드시 0이다.
- upward_m은 **단 하나의 위쪽 포인트**다. m, 0보다 크고 60m 미만, 결측은 null.
- 예시 카메라는 M30 평면 장애물의 간단한 가상 ray cast와 카메라 높이 1m의 바닥 반응으로 만든다.
  위쪽 거리는 천장 4.2m와 센서 높이 2m의 명목 차이 2.2m 주변 합성 변화다.
  미세 변화와 confidence도 합성이며 정밀 광학 시뮬레이션이나 실제 측정이 아니다.
- 원시 예시를 `depth_features(..., rotate=180, confidence=60)`로 처리하면 sample_hyeol과 일치한다.

### 맥 measurement

```json
{"rms":[0.015203,0.016893,0.015203],"mics":[0.6728,0.6911,0.6728]}
```

- 배열 순서는 L/C/R. rms는 디지털 full scale 기준 RMS, mics는 0..1 표시 레벨이다.
- `mics=clip((20*log10(max(rms,1e-7))+70)/50,0,1)`로 현재 맥 송신 코드와 맞췄다.
- 이 데이터에는 원시 PCM이나 저·중·고음 스펙트럼이 없다. 따라서 **주파수 색을 임의로 만들지 않는다.**
- 위치에 따라 음량이 달라지는 두 가상 소리 지점과 시간 변화를 사용했다. 음압/SPL·음원 방향 실측이 아니다.

## M30 → LED 맵핑

**오른쪽 전시실만** X `[-0.462,11.149]`m, Y `[0.090,19.851]`m를 LED 논리
X/Y 0..1 전체로 정규화한다. 기존 렌더러에는 이를 −5..5로 전달한다. 전체 지도는 유지하고
초록 상자로 대응 범위를 표시한다. 같은 경로의 X 표상 폭은 기존보다 약 1.89배 넓어진다. 로비를
센서 데이터로 채우지 않는다. 벽/경로를 LED 배경으로 강제 점등하지 않고 **센서가 관측한 위치**를 표시한다.

지도에서 H/M의 채워진 점은 이 예시의 알려진 위치이고 LED에 대응한다. 빈 원은 현재
미보정 상대 RSSI 방식의 추정 위치이며, 두 값 차이를 숨기지 않는다. 실제 운전은 정답 위치를
알 수 없으므로 이 시뮬레이션 위치를 사용하지 않고 기존 상대 RSSI 입력을 사용한다.

실제 심 탑의 전시장 위치 `(7.646,12.490)`m도 지도에 표기한다. LED 안의 맵은 전시장의
축소된 **논리 공간 표상**이다. +LED X/+LED Y에 +M30 X/+M30 Y를 대응하며,
설치 목업의 heading=183°를 배선 좌표에 자동 회전 적용하지 않는다. 현장 방향 정합은 별도 확인 사항이다.
LED 높이는 혈의 거리·맥의 음량 표시 축이며, 실제 전시장 3D 구조를 복원한 높이가 아니다.

## Activate와 Sleep 재생 규칙

- 경로는 약 46.2m, 60초마다 한 바퀴(약 0.77m/s). 10cm grid에서 벽·기둥·가구·탑을 피하고
  약 70cm 이격한 연결 영역을 사용한다. 두 기구의 경로 진행은 반 바퀴 차이다.
  서로 간 충돌까지 푸는 로봇 제어 경로는 아니다.
- 앞 180초(세 바퀴)는 **예시 전용 임시 DB**의 이전 기억을 준비한다.
- 화면은 마지막 60초를 보여준다. Activate는 현재 관측의 흔적만 찍고 약 0.9초 시간상수로 소멸한다.
- Sleep은 앞의 기억에 **이미 끝난 1초 구간만** 더한다. 미래 이벤트는 미리 합산하지 않는다.
- 혈/맥의 표상은 두 상태에서 동일하다. 혈은 청록 연결 거리 면·상단 보라 천장 표식,
  맥은 따뜻한 색의 L/C/R 음량 기둥이다. 상세는 [sensory-v2](../../docs/SENSOR_MAPPING.md) 규칙이다. Sleep에서만 mean/std와 실제 reservoir 발화·잔광을 합성한다.
- 마지막 8초는 새 관측을 끊어 Activate 소멸과 Sleep 유지가 구분되도록 했다.
- 8,460 뉴런·gain9.5·0.1ms·40step/frame의 기존 발화 기록을 사용한다. **추가 방사형 파동은 없다.**
- 임시 DB는 완료 후 삭제된다. 현장 DB와 수신 전용 설정은 변경하지 않는다. 실제 LED USB도 열지 않는다.

## 재현과 검증

```powershell
python -m ripples.venue_example
# 결과: docs/media/m30_active_sleep.gif / .png / .json
python -m pytest tests/unit/test_venue_example.py -q
```

Python의 `ripples.example_form.validate()`는 JSON 구조 외에 결측 일관성, RMS/레벨 변환,
시간 관계를 검사한다. `load_events()`는 중복·역순 seq/시간을 거부한다.
`to_render_event()`는 단위와 공간 범위를 확인한 뒤 두 표시 상태의 공통 입력으로 바꾼다.
