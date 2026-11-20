# 현재 하드웨어 — 2026-11-12

Nano ESP32(ESP32-S3) 1개, distributed MUX 보드12개, 연결180스트립.
물리 LED10440개/사용10260개, 최대 물리66/사용65개. 모든 첫 물리 LED는 OFF.
Zone1→DD0 … Zone12→DD11, C1→MUX0 … C16→MUX15.
각 MUX 채널을 선택하면 12개 lane을 병렬 전송하고, 16개 채널을 순서대로 갱신한다.
Wi-Fi/BLE OFF, CPU240MHz, 밝기32/255, 모든 lane의 출력 순서는 GRB.
개별 스트립 R/G 보정은 PC에서 처리한다.

프로젝트 전용 FastLED3.10.5 I2S/LCD_CAM 드라이버를 사용한다.
전송 완료+래치300µs 후 MUX 변경, 선택 후 안정화10µs를 확보한다.
MUX는 전원이 아닌 데이터만 전환하며 선택 해제된 LED도 마지막 색을 유지한다.

아래 핀/접점 정보는 사용자가 제공한 회로도 판독 기준이다.
실물 방향/배선은 해당 접점 번호와 대조한다.

## Central 핀맵

`DDn`은 MCU 쪽 데이터 net, `Dn`은 레벨 변환 뒤 유닛으로 가는 net이다. 특히 **회로 net D0과 Nano 핀 D0은 다르다**.

| 유닛 (0 기준) | MCU net | Nano 보드 핀 | 실제 ESP32 GPIO | 레벨 변환 후 | 신호 커넥터 |
|---:|---|---|---:|---|---|
| 0 | DD0 | A1 | 2 | D0 | J13 |
| 1 | DD1 | A3 | 4 | D1 | J14 |
| 2 | DD2 | A6 | 13 | D2 | J15 |
| 3 | DD3 | A7 | 14 | D3 | J16 |
| 4 | DD4 | D2 | 5 | D4 | J17 |
| 5 | DD5 | D3 | 6 | D5 | J18 |
| 6 | DD6 | D9 | 18 | D6 | J19 |
| 7 | DD7 | D8 | 17 | D7 | J20 |
| 8 | DD8 | D7 | 10 | D8 | J21 |
| 9 | DD9 | D6 | 9 | D9 | J22 |
| 10 | DD10 | D5 | 8 | D10 | J23 |
| 11 | DD11 | D4 | 7 | D11 | J24 |

| MUX bit | MCU net | Nano 보드 핀 | 실제 ESP32 GPIO | 출력 net |
|---:|---|---|---:|---|
| 0 (LSB) | SS0 | D10 | 21 | S0 |
| 1 | SS1 | D11 | 38 | S1 |
| 2 | SS2 | D12 | 47 | S2 |
| 3 (MSB) | SS3 | A0 | 1 | S3 |

펌웨어는 FastLED에 **실제 GPIO 번호**를 전달하고, MUX 제어는 ESP-IDF `gpio_*` API로 한다. Arduino IDE에서 **Pin Numbering → By GPIO number (legacy)**를 선택해야 한다. 설치된 FastLED 3.10.5는 Nano의 기본 Arduino 번호 모드에서 코어의 핀 remap 매크로와 충돌하는 것을 컴파일로 확인했다. 일반 `digitalWrite(21, ...)`도 Arduino 번호 모드에서는 다른 핀을 가리킬 수 있으므로 실제 GPIO 숫자와 Nano 숫자를 혼용하면 안 된다.

### Central 레벨 변환기

- U1: SN74AHCT245DWR, VCC=5V, `/OE`=GND, DIR=5V → **A→B**. A1..A4=SS0..SS3, A5..A8=DD0..DD3.
- U2: SN74AHCT245DWR, VCC=5V, `/OE`=GND, DIR=GND → **B→A**. B1..B8=DD6,DD7,DD8,DD9,DD10,DD11,DD5,DD4, A1..A8=D6,D7,D8,D9,D10,D11,D5,D4.
- 두 IC의 방향은 반대지만 회로상 모두 MCU의 3.3V 신호를 분배 보드 쪽 5V 신호로 전달한다.

## 커넥터

Central J13..J24와 distributed J17의 신호 순서:

| 접점 번호 | Central | Distributed |
|---:|---|---|
| 1 | GND | GND |
| 2 | 해당 유닛 Dn | COM |
| 3 | S3 | S3 |
| 4 | S2 | S2 |
| 5 | S1 | S1 |
| 6 | S0 | S0 |

Distributed J1..J16은 각각 I0..I15로 연결된다. LED 커넥터는 1=5V_FUSED, 2=GND, 3=DATA다. 스트립의 **DIN 방향**에 연결한다. Central J1..J12 및 distributed J18 전원 커넥터는 1·2=+5V, 3·4=GND다. 이는 회로 **접점 번호**이며 실물을 보는 방향에 따른 좌우 순서가 아니다.

## Distributed MUX

- U1=CD74HC4067SM, VCC=5V_FUSED, GND=GND, `/E`=GND(항상 활성).
- COM→R17(10Ω)→MUX COM, 각 출력 I0..I15에는 10kΩ 풀다운.
- 선택식: `channel = S0 + 2*S1 + 4*S2 + 8*S3`. 예: I0=0000, I5=0101, I15=1111 (`S3 S2 S1 S0`).
- `/E`를 MCU로 제어할 수 없으므로 전송 중 S선을 바꾸지 않는다. 전송 완료와 LOW 래치 시간을 확보하고 S선을 바꾼 뒤 안정화 시간을 둔다. 여러 S선의 순차 변경 중 잠깐 다른 채널이 선택되어도 데이터가 LOW인 상태로 전환해야 한다.
- 출력 파형·배선 길이·부하에 따른 신호 품질은 오실로스코프로 확인해야 한다. 회로의 풀다운은 선택 해제된 DIN의 LOW 유지를 돕는다.

