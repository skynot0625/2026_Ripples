"""Pi 실행본 (2026-11-07): one composition of the 시각화 작업실 as a folder that runs on the Raspberry Pi 5 of 혈.

The zip holds the same server.py / visual_store.py / tofrender.js / display.html the Mac runs (copied as they are, no
second renderer), a visual.json with only the chosen composition (assigned to the 혈 slots), a manifest (name,
revision, hash, the inputs each layer needs) and short shell scripts. On the Pi the server runs with --standalone (no
simulator: no simulated poses, people or attach switch) and reads the Arducam B0410 itself; Chromium shows
/output/hyeol?clean full screen on the Pi's own monitor. Nothing of the Mac's Python / libraries is copied: install.sh
installs them on the Pi (Arducam's own installer, apt). Inputs the Pi does not have are listed, never replaced by mock
values.

「Pi로 보내기」 (2026-11-09, user: "라즈베리파이에 접속해서 시각화 데이터파일만 업데이트"): the exhibition server keeps the
one composition sent for the Pi (Publish, pi_publish.json next to visual.json); the Pi's server (--follow <exhibition
URL>, from follow.txt in the bundle) asks for it every few seconds and, when it changed, replaces its visual.json and
manifest.json and shows it at once (no restart, no SSH: the Pi only needs to reach the exhibition server).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from visual_store import BUILTINS, DEFAULT_SLOTS, v_composition

FOLDER = "ripples-hyeol-pi"
# what server.py imports and what /output/hyeol loads; nothing else is needed (no venue geometry: test room, no simulator)
APP_FILES = ("server.py", "pi_bundle.py", "visual_store.py", "mapping_store.py", "simulator_store.py", "sim_feed.py", "venue.py",
             "mock_tof.py", "inputs.py", "host_stats.py", "display.html", "tofrender.js", "mapout.js", "testui.js",
             "tofsense.py", "odometry.py")
PORT = 8000
OUTPUT_PATH = "/output/hyeol?clean"   # 혈 블라인드 슬롯, unmapped, one full-screen canvas (display.html)
DEFAULT_FOLLOW = "http://<전시컴-tailscale-이름>:8000"   # 전시컴 (Tailscale) when the page was opened on the server itself

# ToF layer types that place their lines with the simulator's 혈 pose on the Mac (mock only); without a pose
# (the real sensor) they draw in camera space (tofrender.js: simLive.pose is used only when inputs.mock is true)
POSE_RENDERERS = {"tof_spaceline": "공간선", "tof_topdown": "평면 흔적"}
# number sources the Pi reads itself (2026-11-10: the 천장 거리 센서 TOFSense-F2 P on a serial port, tofsense.py)
PI_SOURCES = {"sensor.up_distance": "천장 거리 (TOFSense-F2 P, UART) · Pi에서 직접 읽음"}
# the others: what each one is
SOURCE_NEEDS = {
    "audio.level": "맥 오디오 레벨 · Pi에 없음",
    "hyeol.attached": "혈 사용 중 / 아이들링 신호 · 실제 장치 신호 없음",
    "ai_led.heartbeat": "AI/LED 신호 · 연결 안 됨",
    "ai.learning": "AI 학습 상태 · 연결 안 됨",
}


def _source_need(src: str) -> str:
    if src.startswith("sim."):
        return f"시뮬레이터 위치 ({src}) · 실제 위치 입력 없음"
    if src.startswith("maek.mic"):
        return f"맥 마이크 ({src}) · Pi에 없음"
    return SOURCE_NEEDS.get(src, f"{src} · Pi에 없음")


def requirements(comp: dict) -> list[dict]:
    """Per layer: what it reads and whether the Pi has it. status ok = the Arducam only, missing = an input the Pi
    does not have (the layer shows its no-input state), pose = drawn in camera space only on the Pi (the Mac preview
    places it with the simulator pose), hidden = not drawn."""
    out = []
    for layer in comp["layers"]:
        r, needs, status = layer["renderer"], [], "ok"
        if r.startswith("tof_"):
            needs.append("ToF 깊이 (Arducam B0410) · Pi에서 직접 읽음")
        if r == "tof_spaceline" and layer["params"].get("pose_src", "auto") in ("auto", "tof", "camera"):
            # 2026-11-10: on the Pi 공간선 places its lines with 혈's movement guessed from the ToF itself (odometry.py)
            needs.append("혈 움직임 · ToF 윤곽으로 추정 (Pi에서 직접, 오래 움직이면 조금씩 어긋남 · 4 m 안에 벽 · 물건이 없으면 그동안의 회전은 놓침)"
                         if layer["params"].get("pose_src", "auto") != "camera" else "카메라 기준 (공간에 고정 안 함)")
        elif r in POSE_RENDERERS:
            status = "pose"
            needs.append(f"혈 실제 위치 · 없음 ({POSE_RENDERERS[r]}: 맥 미리보기는 시뮬레이터 위치로 공간에 고정, Pi는 카메라 기준으로만 그림)")
            if r == "tof_topdown" and layer["params"].get("ceil_trace"):
                needs.append(PI_SOURCES["sensor.up_distance"])
        srcs = [layer["source"]] if layer["source"] and layer["source"] != "tof.depth" else []
        srcs += [layer[f]["bind"]["source"] for f in ("opacity", "x", "y", "scale", "rotation") if "bind" in layer[f]]
        for s in dict.fromkeys(srcs):
            if s in PI_SOURCES:
                needs.append(PI_SOURCES[s])
                continue
            needs.append(_source_need(s))
            status = "missing"
        if not layer["visible"]:
            status = "hidden"
        out.append({"id": layer["id"], "name": layer["name"], "renderer": r, "visible": layer["visible"], "status": status, "needs": needs})
    return out


def composition_hash(comp: dict) -> str:
    """The settings as drawn (name, canvas, layers), canonical JSON → 12 hex digits. The Pi checks its copy with it.
    Normalised first (v_composition: settings added later get their defaults), so a composition saved by an older
    version and the same one read back by this version give the same value (2026-11-10: a new default had made the
    Pi's check say 「설정 불일치」)."""
    core = v_composition({"name": comp["name"], "canvas": comp["canvas"], "layers": comp["layers"]})
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]


def _visual_file(comp: dict, revision: int) -> dict:
    cid, core = comp["id"], {"name": comp["name"], "canvas": comp["canvas"], "layers": comp["layers"]}
    slots = dict(DEFAULT_SLOTS, hyeol_device=cid, hyeol_blind=cid)
    comps = {} if cid in BUILTINS else {cid: core}
    return {"version": 2, "revision": revision, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "compositions": comps, "order": list(comps), "slots": slots}


STATUS_TEXT = {"ok": "Pi에서 그대로 작동", "pose": "맥 미리보기와 다르게 보임", "missing": "Pi에 없는 입력 필요", "hidden": "숨김 (그리지 않음)"}


def _readme(m: dict) -> str:
    rows = []
    for layer in m["layers"]:
        rows.append(f"- {layer['name']} ({layer['renderer']}) · {STATUS_TEXT[layer['status']]}")
        rows += [f"  - {n}" for n in layer["needs"]]
    return f"""# Ripples 혈 Pi 실행본

화면: {m['name']} ({m['id']}) · 설정 revision {m['revision']} · 확인값 {m['hash']}
만든 때: {m['created_at']}

설치하는 분은 순서대로 따라 하면 돼요: **1 연결 → 2 설치 → 3 점검 → 4 전시 모드**.
설치가 끝나면 바탕화면에 「혈 시작」 · 「혈 끄기」 · 「혈 점검」 아이콘이 생겨요.

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

1. 이 폴더를 Pi의 홈 폴더로 복사해요. 예: USB로 옮긴 뒤 `~/{FOLDER}`.
2. Pi 데스크톱에서 터미널을 열고:
   `cd ~/{FOLDER} && bash install.sh`
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

{chr(10).join(rows)}

없는 입력은 mock 값으로 바꾸지 않아요. 그 레이어는 입력이 끊겼을 때와 같은 모습(검정)으로 보여요.
천장 거리 미터의 범위는 시각화 작업실의 레이어 설정 「입력 최소 · 최대」(m)예요. 설치 높이에서 천장까지가 그 안에 들어오는지 보세요.

## 화면 설정만 바꾸기 (「Pi로 보내기」)

- 전시컴 시각화 작업실에서 화면을 고르고 「Pi로 보내기」를 누르면, 켜져 있는 Pi가 몇 초 안에 받아 바로 바꿔 그려요. 다시 켜지 않아도 돼요.
- Pi는 `follow.txt`에 적힌 전시컴 주소({m['follow'] or '비어 있음'})에 5초마다 물어봐요. Pi가 그 주소에 닿아야 해요(같은 Tailscale 계정, 또는 같은 공유기에서 전시컴 IP).
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
| 진단 화면 | 브라우저에서 http://127.0.0.1:{PORT}/output/hyeol (상태 글자와 표시 fps) |

## 확인 안 된 것 (실기기에서 아직 안 해 봄)

- Pi 5에서 B0410이 열리는지. Arducam 포럼에 Pi 5 + Bookworm에서 화면이 검게 나온 사례가 2026-05 기준 해결 안 됨으로 남아 있어요.
- TOFSense-F2 P 실물 연결 (프레임 형식은 제조사 NLink 프로토콜 · ArduPilot 구현과 같게 맞췄고, 가짜 센서로만 확인).
- 표시 fps, 이 화면의 WebGL 레이어가 Pi 5 Chromium에서 그려지는지.
- 전시 모드 자동 시작(데스크톱 자동 시작 항목)이 Pi OS 버전마다 동작하는지.
"""


SETTINGS_SH = r"""# 혈 Pi 설정 — 고친 뒤 「혈 끄기」 → 「혈 시작」 (또는 bash stop.sh; bash start.sh)
# 카메라 번호. 안 열리면 8
CAMERA_INDEX=0
# 천장 거리 센서 포트: auto (USB 어댑터 → GPIO 8·10번 핀) / /dev/ttyAMA0 (GPIO 핀) / /dev/ttyUSB0 (USB 어댑터)
DISTANCE_PORT=auto
# 센서 속도: 0 = 자동 (921600 공장값부터)
DISTANCE_BAUD=0
# 화면 오른쪽 아래 점검 칸 (fps · 카메라 · 천장 거리 · 혈 움직임 · Pi 온도/전원): 1 = 켜고 시작, 0 = 끄고 시작.
# 화면에서 키보드 D 로 언제든 켜고 끔. bash exhibition.sh on 하면 0 으로 바뀜
TEST_PANEL=1
"""

INSTALL_SH = r"""#!/bin/bash
# Pi 5에서 한 번 실행. 맥의 라이브러리를 복사하지 않고 Pi에서 설치한다.
set -e
cd "$(dirname "$0")"
HERE="$PWD"
echo "== 1/4 ToF 카메라 (Arducam SDK · 드라이버 · numpy · opencv)"
if [ ! -d "$HOME/Arducam_tof_camera" ]; then
  git clone https://github.com/ArduCAM/Arducam_tof_camera.git "$HOME/Arducam_tof_camera"
fi
(cd "$HOME/Arducam_tof_camera" && ./Install_dependencies.sh)
echo "== 2/4 전체 화면 브라우저 (Bookworm: chromium-browser, Trixie: chromium)"
command -v chromium >/dev/null || command -v chromium-browser >/dev/null || sudo apt install -y chromium || sudo apt install -y chromium-browser
echo "== 3/4 천장 거리 센서용 직렬 포트 (GPIO 8·10번 핀)"
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
grep -q '^enable_uart=1' "$CFG" || echo 'enable_uart=1' | sudo tee -a "$CFG" >/dev/null
grep -q '^dtparam=uart0=on' "$CFG" || echo 'dtparam=uart0=on' | sudo tee -a "$CFG" >/dev/null   # Pi 5: GPIO 14/15 = /dev/ttyAMA0
sudo raspi-config nonint do_serial_cons 1 || true    # 직렬 콘솔 끄기 (센서가 같은 핀을 씀)
sudo usermod -aG dialout "$USER"                      # 직렬 포트 읽기 권한
echo "== 4/4 바탕화면 아이콘"
chmod +x "$HERE"/*.sh
mkdir -p "$HOME/Desktop"
icon() {   # 이름, 명령, 터미널 창(true/false)
  cat > "$HOME/Desktop/$1.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$1
Exec=bash -c 'cd "$HERE" && $2'
Terminal=$3
Icon=utilities-terminal
EOF
  chmod +x "$HOME/Desktop/$1.desktop"
}
icon "혈 시작" "bash start.sh" false
icon "혈 끄기" "bash stop.sh" false
icon "혈 점검" "bash check.sh; read -p '엔터를 누르면 닫혀요'" true
echo
echo "설치 끝. 재부팅(sudo reboot)한 뒤 바탕화면 「혈 점검」(또는 bash check.sh)으로 확인하세요."
"""

CHECK_SH = r"""#!/bin/bash
cd "$(dirname "$0")"
. ./settings.sh
if [ -f run/server.pid ] && kill -0 "$(cat run/server.pid)" 2>/dev/null; then
  # 켜져 있는 동안은 카메라 · 센서를 서버가 쓰고 있으니, 서버에 물어본다
  python3 app/pi_check.py --running
else
  python3 app/pi_check.py --camera --camera-index "$CAMERA_INDEX" --distance "$DISTANCE_PORT" --distance-baud "$DISTANCE_BAUD"
fi
# 화면 해상도 (Wayland: wlr-randr, X: xrandr)
if command -v wlr-randr >/dev/null; then
  R=$(wlr-randr 2>/dev/null | grep -m1 'current' | awk '{print $1}')
elif command -v xrandr >/dev/null; then
  R=$(xrandr 2>/dev/null | grep -m1 '\*' | awk '{print $1}')
fi
if [ -n "$R" ]; then
  [ "$R" = "1920x1280" ] && echo "화면 $R" || echo "화면 $R · 이 화면은 1920x1280 에 맞춰 만들었어요 (모니터를 HDMI 0에)"
else
  echo "화면 해상도를 못 읽음 (데스크톱 터미널에서 실행하세요)"
fi
"""

START_SH = r"""#!/bin/bash
# 서버(실제 ToF · 천장 거리 센서, 시뮬레이터 없음)를 켜고 혈 화면을 전체 화면으로 띄운다.
cd "$(dirname "$0")"
. ./settings.sh
mkdir -p run
python3 app/pi_check.py || exit 1
if [ -f run/server.pid ] && kill -0 "$(cat run/server.pid)" 2>/dev/null; then
  echo "서버가 이미 켜져 있음 (bash stop.sh 로 끄고 다시)"; exit 1
fi
# 「Pi로 보내기」를 따라갈 전시컴 주소 (follow.txt 첫 줄, 비우면 따라가지 않음)
FOLLOW=$(grep -v '^#' follow.txt 2>/dev/null | head -1 | tr -d '[:space:]')
BAUD=""; [ "${DISTANCE_BAUD:-0}" != "0" ] && BAUD="--distance-baud $DISTANCE_BAUD"
python3 app/server.py --standalone --host 127.0.0.1 --port __PORT__ --camera-index "${CAMERA_INDEX:-0}" \
  --distance tofsense --distance-port "${DISTANCE_PORT:-auto}" $BAUD \
  --visual-file "$PWD/data/visual.json" --mapping-file "$PWD/data/mapping.json" ${FOLLOW:+--follow "$FOLLOW"} >> run/server.log 2>&1 &
echo $! > run/server.pid
for i in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:__PORT__/status && break; sleep 0.25; done
curl -s -o /dev/null http://127.0.0.1:__PORT__/status || { echo "서버 시작 실패 · run/server.log 확인"; exit 1; }
BROWSER=$(command -v chromium || command -v chromium-browser)
"$BROWSER" --kiosk --noerrdialogs --disable-infobars --no-first-run --password-store=basic --disable-session-crashed-bubble \
  --user-data-dir="$PWD/run/chromium" "http://127.0.0.1:__PORT____OUTPUT__&panel=${TEST_PANEL:-0}" >> run/browser.log 2>&1 &
echo $! > run/browser.pid
sleep 3
python3 app/pi_check.py --running --quiet
echo "시작함 · 끄기: bash stop.sh (또는 바탕화면 「혈 끄기」)"
"""

STOP_SH = r"""#!/bin/bash
cd "$(dirname "$0")"
for f in run/browser.pid run/server.pid; do
  [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null
  rm -f "$f"
done
echo "종료함"
"""

EXHIBITION_SH = r"""#!/bin/bash
# 전시 모드: 전원만 켜면 혈 화면이 자동으로 뜬다. bash exhibition.sh on | off | (상태)
cd "$(dirname "$0")"
HERE="$PWD"
AUTO="$HOME/.config/autostart/ripples-hyeol.desktop"
case "$1" in
  on)
    sudo raspi-config nonint do_boot_behaviour B4 || echo "자동 로그인 설정 실패 · raspi-config 에서 직접 (System → Boot / Auto Login → Desktop Autologin)"
    sudo raspi-config nonint do_blanking 1 || echo "화면 꺼짐 끄기 실패 · raspi-config 에서 직접 (Display → Screen Blanking → No)"
    sed -i 's/^TEST_PANEL=.*/TEST_PANEL=0/' settings.sh   # 전시 때는 점검 칸 없이 (D 키로는 여전히 켤 수 있음)
    mkdir -p "$(dirname "$AUTO")"
    cat > "$AUTO" <<EOF
[Desktop Entry]
Type=Application
Name=Ripples 혈 (전시 모드)
Exec=bash -c 'mkdir -p "$HERE/run"; sleep 8; bash "$HERE/start.sh" >> "$HERE/run/autostart.log" 2>&1'
X-GNOME-Autostart-enabled=true
EOF
    echo "전시 모드 켬 · 재부팅하면 혈 화면이 자동으로 떠요: sudo reboot"
    echo "(자동 시작 기록: run/autostart.log)" ;;
  off)
    rm -f "$AUTO"
    sed -i 's/^TEST_PANEL=.*/TEST_PANEL=1/' settings.sh
    echo "전시 모드 끔 (부팅 때 자동 시작 안 함, 점검 칸 다시 켬). 자동 로그인 · 화면 꺼짐 설정은 그대로 둠" ;;
  *)
    [ -f "$AUTO" ] && echo "전시 모드: 켜짐" || echo "전시 모드: 꺼짐"
    echo "켜기: bash exhibition.sh on   끄기: bash exhibition.sh off" ;;
esac
"""

PI_CHECK = r'''#!/usr/bin/env python3
"""Pi 실행본 확인: 설정 확인값 (manifest = visual.json), 라이브러리, (--camera) Arducam 5초 측정, (--distance) 천장 거리
센서, (--running) 켜져 있는 서버에 카메라 · 센서 상태를 물어봄. 서버가 켜져 있을 때 --camera / --distance 는 쓰지 않는다
(카메라 · 직렬 포트는 한 프로그램만 쓴다: check.sh 가 알아서 고름)."""
import argparse, json, sys, time, urllib.request
from pathlib import Path

here = Path(__file__).resolve().parent
root = here.parent
sys.path.insert(0, str(here))
ap = argparse.ArgumentParser()
ap.add_argument("--camera", action="store_true")
ap.add_argument("--camera-index", type=int, default=0)
ap.add_argument("--distance", default="", help="천장 거리 센서 포트 (auto / 경로); 비우면 확인 안 함")
ap.add_argument("--distance-baud", type=int, default=0)
ap.add_argument("--running", action="store_true", help="켜져 있는 서버(127.0.0.1:8000)에 물어봄")
ap.add_argument("--quiet", action="store_true", help="--running 의 입력 두 줄만")
a = ap.parse_args()
ok = True


def running() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/status", timeout=3) as r:
            st = json.loads(r.read())
        with urllib.request.urlopen("http://127.0.0.1:8000/inputs?format=json", timeout=3) as r:
            raw = r.read()
        try:
            ch = json.loads(raw).get("channels", {}).get("distance", {})
        except ValueError:
            ch = {}
    except Exception as exc:
        print(f"서버에 닿지 않음: {exc}"); return False
    cam = f"ToF 카메라: {st.get('resolution')} · {st.get('fps')} fps" if st.get("connected") else f"ToF 카메라: 안 열림 · {st.get('error')}"
    print(cam)
    if ch.get("connected") and isinstance(ch.get("value"), (int, float)):
        print(f"천장 거리 센서: 지금 {ch['value']:.2f} m · {ch.get('rate_hz')} Hz")
    elif ch.get("connected"):
        print("천장 거리 센서: 연결됨 · 측정값 없음 (위가 너무 멀거나 가까움)")
    else:
        print(f"천장 거리 센서: 값 없음 · {ch.get('error') or '연결 안 됨'} (run/server.log)")
    return bool(st.get("connected"))


if a.running:
    sys.exit(0 if running() else 1)

m = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
import pi_bundle
from visual_store import BUILTINS, VisualStore
v = json.loads((root / "data" / "visual.json").read_text(encoding="utf-8"))
cid = v["slots"]["hyeol_blind"]
store = VisualStore.__new__(VisualStore)
comp = dict(BUILTINS[cid]) if cid in BUILTINS else VisualStore._parse(store, v)["compositions"][cid]
comp["id"] = cid
h = pi_bundle.composition_hash(comp)
print(f"화면 {m['name']} ({m['id']}) · revision {m['revision']} · 확인값 {m['hash']}")
if h != m["hash"] or cid != m["id"]:
    print(f"설정 불일치: data/visual.json 확인값 {h} (manifest {m['hash']})"); ok = False
else:
    print("설정 일치")
for name in ("numpy", "cv2", "ArducamDepthCamera"):
    try:
        __import__(name); print(f"{name} 있음")
    except Exception as exc:
        print(f"{name} 없음: {exc}"); ok = False
if a.camera and ok:
    import ArducamDepthCamera as ac
    cam = ac.ArducamCamera()
    r = cam.open(ac.Connection.CSI, a.camera_index)
    if r != 0:
        print(f"카메라 열기 실패 ({r}) · index {a.camera_index} · 리본 케이블 · CAM/DISP 0 · 카메라 전원, settings.sh 의 CAMERA_INDEX=8"); ok = False
    else:
        try:
            if cam.start(ac.FrameType.DEPTH) != 0:
                print("카메라 시작 실패"); ok = False
            else:
                info = cam.getCameraInfo()
                n, t0 = 0, time.monotonic()
                while time.monotonic() - t0 < 5:
                    f = cam.requestFrame(2000)
                    if f is not None:
                        n += 1; cam.releaseFrame(f)
                print(f"카메라 {info.width}x{info.height} · 5초 동안 {n}프레임 · 수집 {n / 5:.1f} fps (표시 fps는 화면 진단에서 따로 확인)")
                cam.stop()
        finally:
            cam.close()
if a.distance:
    import tofsense
    found = tofsense.find(a.distance, a.distance_baud or None, seconds=1.5)
    if found is None:
        ports = tofsense.candidate_ports(a.distance)
        if not ports:
            print("천장 거리 센서: 포트 없음 · install.sh 뒤 재부팅했는지, 또는 USB-UART 어댑터를 꽂았는지");
        else:
            print(f"천장 거리 센서: 신호 없음 (찾아본 포트 {', '.join(ports)}) · TX↔RX 엇갈림 · 5V · GND · 센서가 UART 모드인지 (README 1. 연결)")
        ok = False
    else:
        import os, select
        path, baud, fd, frames = found
        parser, vals, t0 = tofsense.FrameParser(), [f["m"] for f in frames], time.monotonic()
        while time.monotonic() - t0 < 2:
            if select.select([fd], [], [], 0.2)[0]:
                vals += [f["m"] for f in parser.feed(os.read(fd, 1024))]
        os.close(fd)
        good = [x for x in vals if x is not None]
        rate = len(vals) / (time.monotonic() - t0 + 1e-6)
        now = f"지금 {good[-1]:.2f} m" if good else "측정값 없음 (위가 너무 멀거나 가까움)"
        print(f"천장 거리 센서: {path} · {baud} baud · {now} · 약 {rate:.0f} Hz · 유효 {len(good)}/{len(vals)}")
for layer in m["layers"]:
    if layer["status"] in ("missing", "pose"):
        print(f"주의 · {layer['name']}: " + " / ".join(layer["needs"][1:] if layer["needs"][:1] and layer["needs"][0].startswith("ToF") else layer["needs"]))
sys.exit(0 if ok else 1)
'''


def build(page_dir: Path, comp: dict, revision: int, follow: str = "") -> tuple[bytes, str, dict]:
    """→ (zip bytes, download file name, manifest). follow = the exhibition server the Pi asks for 「Pi로 보내기」."""
    files = {f"app/{n}": (page_dir / n).read_bytes() for n in APP_FILES}
    files["app/pi_check.py"] = PI_CHECK.encode("utf-8")
    files["data/visual.json"] = json.dumps(_visual_file(comp, revision), ensure_ascii=False, indent=2).encode("utf-8")
    manifest = {"id": comp["id"], "name": comp["name"], "builtin": bool(comp.get("builtin")), "revision": revision,
                "hash": composition_hash(comp), "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "output": OUTPUT_PATH, "port": PORT, "follow": follow, "layers": requirements(comp),
                "files": {k: hashlib.sha256(b).hexdigest()[:16] for k, b in sorted(files.items())}}
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    files["follow.txt"] = f"{follow}\n# 「Pi로 보내기」를 받을 전시컴 주소 (첫 줄). 비우면 따라가지 않음. 고친 뒤 bash stop.sh, bash start.sh\n".encode("utf-8")
    files["README.md"] = _readme(manifest).encode("utf-8")
    scripts = {"install.sh": INSTALL_SH, "check.sh": CHECK_SH, "start.sh": START_SH.replace("__PORT__", str(PORT)).replace("__OUTPUT__", OUTPUT_PATH),
               "stop.sh": STOP_SH, "exhibition.sh": EXHIBITION_SH, "settings.sh": SETTINGS_SH}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        stamp = datetime.now().timetuple()[:6]
        for name, data, mode in [*((n, d, 0o644) for n, d in files.items()), *((n, t.encode("utf-8"), 0o755) for n, t in scripts.items())]:
            info = zipfile.ZipInfo(f"{FOLDER}/{name}", date_time=stamp)
            info.external_attr, info.compress_type = mode << 16, zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", comp["name"]).strip("_") or comp["id"]
    return buf.getvalue(), f"{FOLDER}_{safe}_r{revision}.zip", manifest


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Publish:
    """「Pi로 보내기」 on the exhibition server: the composition the 혈 Pi follows. Kept in pi_publish.json (its own
    file next to visual.json; nothing else is written). seen = the Pi's last question (when, which hash it had)."""

    def __init__(self, path: Path) -> None:
        self.path, self.lock, self.seen = path, threading.Lock(), None
        try:
            self.doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.doc = None

    def publish(self, comp: dict, revision: int) -> dict:
        doc = {"id": comp["id"], "name": comp["name"], "builtin": bool(comp.get("builtin")), "revision": revision,
               "hash": composition_hash(comp), "published_at": _now(), "layers": requirements(comp),
               "visual": _visual_file(comp, revision)}
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(self.path, doc)
            self.doc = doc
        return self.status()

    def status(self) -> dict:
        with self.lock:
            d = self.doc
            meta = None if d is None else {k: d[k] for k in ("id", "name", "revision", "hash", "published_at")}
            return {"published": meta, "seen": self.seen}

    def ask(self, have: str, who: str) -> dict | None:
        """The Pi's question. None = nothing sent yet; {"same": True} = it already has it; else the whole document."""
        with self.lock:
            self.seen = {"at": _now(), "hash": have, "from": who}
            if self.doc is None:
                return None
            return {"same": True, "hash": have} if have == self.doc["hash"] else dict(self.doc)


def current_hash(visual) -> str:
    comp = visual.composition(visual.snapshot()["slots"]["hyeol_blind"])
    return composition_hash(comp) if comp else ""


def follow(url: str, visual, manifest_path: Path, stop: threading.Event, every: float = 1.0) -> threading.Thread:
    """Pi side of 「Pi로 보내기」: ask <url>/api/pi-publish every few seconds; a changed composition replaces
    visual.json (VisualStore.replace: the screen redraws at once) and the manifest (so pi_check.py still agrees).
    The exhibition server being out of reach changes nothing: the last received settings stay."""
    base = url.rstrip("/")

    def run() -> None:
        last = None   # last printed state, so the log has one line per change
        while not stop.is_set():
            have = current_hash(visual)
            try:
                with urllib.request.urlopen(f"{base}/api/pi-publish?have={quote(have)}", timeout=5) as r:
                    doc = json.loads(r.read())
                if doc.get("same"):
                    state = f"같음 {have}"
                else:
                    visual.replace(doc["visual"])
                    got = current_hash(visual)
                    if got != doc["hash"]:
                        raise ValueError(f"받은 설정 확인값 {got} ≠ {doc['hash']}")
                    if manifest_path.exists():
                        m = json.loads(manifest_path.read_text(encoding="utf-8"))
                        m.update({k: doc[k] for k in ("id", "name", "builtin", "revision", "hash", "layers")}, received_at=_now())
                        _write_json(manifest_path, m)
                    state = f"받음 {doc['name']} r{doc['revision']} {doc['hash']}"
                    print(f"Pi로 보내기: {state} ({_now()})", flush=True)
            except urllib.error.HTTPError as exc:
                state = "전시컴에 보낸 화면 없음" if exc.code == 404 else f"전시컴 응답 {exc.code}"
            except (OSError, ValueError, KeyError, TypeError) as exc:
                state = f"전시컴에 닿지 않음 · {exc}"
            if state != last:
                if not state.startswith("받음"):
                    print(f"Pi로 보내기: {state} ({_now()})", flush=True)
                last = state
            stop.wait(every)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
