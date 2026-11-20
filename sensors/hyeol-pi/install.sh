#!/bin/bash
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
