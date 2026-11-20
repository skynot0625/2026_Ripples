# Ripples 혈 Pi 실행본

화면: test3.1 (c3) · 설정 revision 1093 · 확인값 a7de9d590131
만든 때: 2026-11-10 (수집단 병합본 + r1093: 공간선 신뢰도 30 · 잡음 거르기 2)

> 이 묶음 = 수집단(collector) 병합본에 화면 r1093(tofrender.js · visual_store.py · data/visual.json · manifest.json)을 넣은 것.
> ToF 잡음 점검: `tof_check.py` (맨 위 설명대로, 수집단을 끄고 실행).

설치하는 분은 순서대로 따라 하면 돼요: **1 연결 → 2 설치 → 3 점검 → 4 전시 모드**.
설치가 끝나면 바탕화면에 「혈 시작」 · 「혈 끄기」 · 「혈 점검」 아이콘이 생겨요.

## 0. 구조 (2026-11-10 개편: 수집단 / 표시단 분리)

```
센서 ─▶ 수집단 collector (부팅 서비스 ripples-collector) ─┬─▶ 파일 ~/ripples-data  (형식: collector/DATA_FORMAT.md)
                                                          └─▶ run/collector.sock ─▶ 표시단 server.py + Chromium (혈 화면)
```

- **센서를 여는 건 수집단 하나뿐**이에요. 표시단(이 폴더의 혈 화면)은 `--source collector --distance collector`로 소켓에서 받아요.
  화면을 끄고 켜도(`stop.sh`/`start.sh`) 수집 · 저장은 계속돼요.
- 설치: 맥에서 `bash deploy.sh` (Pi Wi-Fi `ripples-direct` 연결 상태) → Pi에서 `sudo reboot`.
  Pi에서 직접 하려면 `bash setup_pi.sh`. (이 Pi는 인터넷이 없어서 아래 「2. 설치」의 install.sh는 쓰지 않아요)
- 상태: `bash check.sh` 맨 위 「수집단」 줄들 · `sudo systemctl status ripples-collector` · `journalctl -u ripples-collector -f`
- 저장 설정: `settings.sh` 아래쪽 (`DATA_DIR`, `RECORD`, `TOF_SAVE_FPS`(all=raw 전부), `TOF_CHUNK_SECONDS`, `TOF_COMPRESS`, `DATA_BUDGET_GB`, `MIN_FREE_GB`) → `sudo systemctl restart ripples-collector`
- 용량 실측 · 예측: `~/tof-env/bin/python collector/disk_report.py`
- 이 Pi는 **Raspberry Pi 4B**예요 (아래 표의 Pi 5 안내 중 포트 위치는 Pi 4B도 같음). 천장 거리 센서는 GPIO 2번(5V) · 14번(GND) · 8번(TX) · 10번(RX).

## 1. 연결

| 무엇 | Pi 5 어디에 | 메모 |
|---|---|---|
| 모니터 (ZSUS 10.5″, 1920×1280) | **HDMI 0** (USB-C 전원 단자 바로 옆 micro HDMI) | 모니터 전원은 모니터의 USB-C에 따로. 화면 크기는 1920×1280에 맞춰 만들었어요 |
| ToF 카메라 Arducam B0410 | **CAM/DISP 0** (22핀 리본 케이블) | 카메라에 따로 5V 2A 전원. 케이블 금속 면 방향 확인 |
| 천장 거리 센서 TOFSense-F2 P | 아래 **40핀 4개** (또는 USB-UART 어댑터) | 센서는 위(천장)를 보게 |
| Pi 전원 | USB-C 27W (5V 5A) | |

### 천장 거리 센서 배선 (40핀)

센서 케이블 4가닥: **VCC · GND · TX · RX**. 색이 제품마다 다르니(빨강이 GND인 제품도 있음) **센서 기판의 글자**로 확인하세요.

```
            Pi 5 40핀 (보드 위에서, 1번 핀 = 네모 납땜 자국, SD 카드 쪽 끝)
                 홀수 ┌───┐ 짝수
                   1  │o o│  2   5V  ──────── 센서 VCC
                   3  │o o│  4
                   5  │o o│  6   GND ──────── 센서 GND
                   7  │o o│  8   TXD (GPIO14) ── 센서 RX
                   9  │o o│ 10   RXD (GPIO15) ── 센서 TX
                      │...│
```

- **TX ↔ RX는 엇갈려** 꽂아요 (Pi 8번 → 센서 RX, Pi 10번 → 센서 TX).
- 센서 신호는 3.3V라 Pi 핀에 바로 꽂아도 돼요. 전원만 5V.
- 핀 대신 **USB-UART 어댑터**(3.3V, CP2102 · CH340 등)로 USB에 꽂아도 돼요. 어댑터 TX → 센서 RX, RX → 센서 TX, GND, 5V. 프로그램이 USB를 먼저 찾아요.
- 센서는 공장 설정(UART 능동 출력, 921600 baud) 그대로 써요. NAssistant로 I2C · CAN으로 바꿨다면 UART로 되돌려 주세요.

## 2. 설치 (처음 한 번, 인터넷 필요)

1. 이 폴더를 Pi의 홈 폴더로 복사해요. 예: USB로 옮긴 뒤 `~/ripples-hyeol-pi`.
2. Pi 데스크톱에서 터미널을 열고:
   `cd ~/ripples-hyeol-pi && bash install.sh`
   - ToF 카메라 드라이버, 브라우저, **직렬 포트(GPIO 8·10번 핀) 켜기**, 바탕화면 아이콘까지 해요.
3. 끝나면 재부팅: `sudo reboot`

## 3. 점검

바탕화면 「혈 점검」, 또는 `bash check.sh`. 이렇게 나오면 정상이에요:

- `설정 일치`
- `카메라 240x180 · … fps`
- `천장 거리 센서: /dev/… · 921600 baud · 지금 2.2 m` (센서 위가 막혀 있으면 그 거리)
- `화면 1920x1280`

안 될 때 → 아래 「문제 해결」.

## 4. 시작 · 끄기 · 전시 모드

- 시작: 바탕화면 「혈 시작」 (또는 `bash start.sh`). 서버가 켜지고 혈 화면이 전체 화면으로 떠요.
- 끄기: 「혈 끄기」 (또는 `bash stop.sh`). 전체 화면에서 나오려면 키보드 Alt+F4도 돼요.
- **전시 모드** (전원만 켜면 혈 화면이 자동으로 뜸): `bash exhibition.sh on` 후 재부팅.
  데스크톱 자동 로그인 · 화면 꺼짐 방지 · 부팅 때 자동 시작을 한 번에 켜요. 끄기: `bash exhibition.sh off`, 상태: `bash exhibition.sh`.
- 전시 때는 마우스를 빼 두세요(화면에 커서가 보일 수 있어요).

### 점검 칸 (화면 오른쪽 아래)

설치하고 처음 켜면 화면 오른쪽 아래에 작은 점검 칸이 떠요. **키보드 D**로 숨기고 다시 띄워요(한글 자판이어도 같은 키).
`bash exhibition.sh on` 하면 다음부터는 숨긴 채로 시작해요(`settings.sh`의 `TEST_PANEL`).

| 줄 | 정상 | 아닐 때 |
|---|---|---|
| 화면 | `1920×1280 · 맞음` (초록) | 노랑 = 모니터 해상도가 다름 → HDMI 0, 모니터 설정 |
| 표시 | 15 fps 이상 | 빨강 = 화면이 멈춤 · 너무 느림 |
| ToF 카메라 | `240x180 · 수집 30 fps` | 「안 열림」 → 리본 케이블 · 카메라 전원 · `CAMERA_INDEX` |
| 천장 거리 | `2.20 m · 50 Hz · TOFSense-F2 P` | 빨강 글자에 이유가 나옴 (포트 없음 / 신호 없음) → 1. 연결 |
| 혈 움직임 | `방향 …° · 맞춤 80%` | 노랑 「맞출 윤곽 없음」 = 4 m 안에 벽 · 물건이 없음 (고장 아님, 그동안 회전은 추정 못 함) |
| Pi | `CPU …% · …°C · 전원 정상` | 「전원 부족!」 = 27W(5V 5A) 어댑터로 · 「열로 느려짐!」 = 방열판 · 팬 |

## 설정 바꾸기 (`settings.sh`)

텍스트 편집기로 열어 고친 뒤 끄고 다시 시작해요.

- `CAMERA_INDEX=0` · 카메라가 안 열리면 8 (Pi 5에서 다른 번호를 쓴 사례, 미확인)
- `DISTANCE_PORT=auto` · 자동(USB 어댑터 → GPIO 핀 순서). 직접 지정: `/dev/ttyAMA0`(GPIO 핀), `/dev/ttyUSB0`(어댑터)
- `DISTANCE_BAUD=0` · 0 = 921600(공장값) → 115200 → 460800 → 230400 순서로 찾아요

## 레이어와 필요한 입력

- 공간선(실험) · ToF 3D 선 (누적·처짐·초점) (tof_spaceline) · Pi에서 그대로 작동
  - ToF 깊이 (Arducam B0410) · Pi에서 직접 읽음
  - 혈 움직임 · ToF 윤곽으로 추정 (Pi에서 직접, 오래 움직이면 조금씩 어긋남 · 4 m 안에 벽 · 물건이 없으면 그동안의 회전은 놓침)
- ToF SENSOR (깊이 흑백) (tof_sensor) · 숨김 (그리지 않음)
  - ToF 깊이 (Arducam B0410) · Pi에서 직접 읽음
- VERTICAL METER 왼쪽 · 천장 거리(높이) (updist_meter) · Pi에서 그대로 작동
  - 천장 거리 (TOFSense-F2 P, UART) · Pi에서 직접 읽음
- VERTICAL METER 오른쪽 · 천장 거리(높이) (updist_meter) · Pi에서 그대로 작동
  - 천장 거리 (TOFSense-F2 P, UART) · Pi에서 직접 읽음

없는 입력은 mock 값으로 바꾸지 않아요. 그 레이어는 입력이 끊겼을 때와 같은 모습(검정)으로 보여요.
천장 거리 미터의 범위는 시각화 작업실의 레이어 설정 「입력 최소 · 최대」(m)예요. 설치 높이에서 천장까지가 그 안에 들어오는지 보세요.

## 화면 설정만 바꾸기 (「Pi로 보내기」)

- 전시컴 시각화 작업실에서 화면을 고르고 「Pi로 보내기」를 누르면, 켜져 있는 Pi가 몇 초 안에 받아 바로 바꿔 그려요. 다시 켜지 않아도 돼요.
- Pi는 `follow.txt`에 적힌 전시컴 주소(http://<전시컴-tailscale-이름>:8000)에 5초마다 물어봐요. Pi가 그 주소에 닿아야 해요(같은 Tailscale 계정, 또는 같은 공유기에서 전시컴 IP).
- 주소를 바꾸려면 `follow.txt` 첫 줄을 고치고 끄고 다시 시작. 비워 두면 따라가지 않아요.
- 받은 설정은 `data/visual.json`과 `manifest.json`에 저장돼서, 전시컴에 닿지 않을 때나 다시 켤 때도 마지막으로 받은 화면을 그려요.

## 문제 해결

| 증상 | 확인 |
|---|---|
| 좌우 미터가 검정 | `bash check.sh` 의 천장 거리 센서 줄. 「포트 없음」 = install.sh 뒤 재부팅했는지 / USB 어댑터. 「신호 없음」 = TX↔RX 엇갈림, 5V · GND, 센서가 UART 모드인지 |
| 가운데 그림이 안 나옴 | check.sh 카메라 줄. 리본 케이블 방향 · CAM/DISP 0 · 카메라 전원. `CAMERA_INDEX=8` |
| 화면이 작거나 잘림 | check.sh 화면 줄이 1920x1280인지. 모니터를 HDMI 0에 |
| 무엇이 문제인지 모를 때 | 화면에서 D 키 → 오른쪽 아래 점검 칸의 빨강 · 노랑 줄 |
| 실행 기록 | `run/server.log` (센서를 찾은 포트, 끊김) · `run/browser.log` |
| 진단 화면 | 브라우저에서 http://127.0.0.1:8000/output/hyeol (상태 글자와 표시 fps) |

## 확인 안 된 것 (실기기에서 아직 안 해 봄)

- Pi 5에서 B0410이 열리는지. Arducam 포럼에 Pi 5 + Bookworm에서 화면이 검게 나온 사례가 2026-05 기준 해결 안 됨으로 남아 있어요.
- TOFSense-F2 P 실물 연결 (프레임 형식은 제조사 NLink 프로토콜 · ArduPilot 구현과 같게 맞췄고, 가짜 센서로만 확인).
- 표시 fps, 이 화면의 WebGL 레이어가 Pi 5 Chromium에서 그려지는지.
- 전시 모드 자동 시작(데스크톱 자동 시작 항목)이 Pi OS 버전마다 동작하는지.
