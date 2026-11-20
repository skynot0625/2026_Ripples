# 맥 → 심 실시간 값 형식 (v1, 2026-11-18)

맥 Pi가 초당 10번(`device.json`의 `sim_hz`) 심 서버에 JSON 한 개를 보낸다.

- 주소: `POST http://<심 PC>:8000/api/maek` (`device.json`의 `sim_url`. 공유기 망(2026-11-20~) `http://192.168.0.2:8000/api/maek`, 예전 전시 PC 핫스팟은 `http://192.168.137.1:8000/api/maek`)
- 본문: `Content-Type: application/json`, 4KB 이하. 성공하면 204(본문 없음), 형식이 틀리면 400 + `{"error": …}`
- 실시간 값만 보낸다. 실패한 것은 다시 보내지 않는다(늦은 값은 의미가 없음). 오디오 처리와 따로 돌아서 서버가 없어도 소리·진동은 그대로.
- 예시: `examples/sim_maek_sample.json`

| 키 | 형 | 뜻 |
|---|---|---|
| `v` | 1 | 형식 판. 바뀌면 올린다 |
| `device` | 문자열 | 기기 이름(`audio.ini` `[server] device_id`) |
| `seq` | 정수 | 보낸 순서. 빠진 번호 = 보내다 놓친 것 |
| `t` | 초(소수) | Pi 시계 기준 보낸 시각. **Pi 시계는 인터넷이 없으면 틀릴 수 있어** 순서·간격용으로만 |
| `mics` | [왼, 가운데, 오른] 0–1 | 마이크 레벨. 대역 통과 RMS를 dB로 바꿔 −70 dBFS → 0, −20 dBFS → 1 (`sim_level_db`). **음압(dB SPL) 보정 아님** |
| `rms` | [왼, 가운데, 오른] | 위 값의 원래 RMS(디지털 풀스케일 1 기준) |
| `haptic` | [왼, 오른] 0–1 | 지금 진동모터 목표 세기(진동 곡선·감도 반영 후) |
| `level_bias` | −1…1 | 좌우 레벨 차(−1 왼쪽, +1 오른쪽). 각도 아님 |
| `direction_deg` | 숫자 또는 null | 소리 방향(기구 앞 0°, 오른쪽 +). 마이크 위치·동기화가 확인되기 전에는 늘 null |

심 서버에서는 기존 가짜 입력과 같은 자리를 채운다: `/inputs`의 `channels.maek_mic.value` = `mics`, `channels.maek_features.value` = 방향·진동·RMS. 진짜 값이 3초 안에 들어오는 동안 가짜 입력은 멈춘다. `source`가 `maek-pi:<device>`면 진짜 값.
