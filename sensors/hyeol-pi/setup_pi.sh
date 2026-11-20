#!/bin/bash
# Pi에서 한 번: 수집단을 부팅 서비스로 등록하고, 표시단(혈 화면)을 데스크톱 자동 시작으로 켠다.
# 인터넷 필요 없음 (Pi에 이미 있는 ~/tof-env 를 씀). 맥에서는 deploy.sh 가 복사 + 이 스크립트 실행을 한 번에 함.
set -e
cd "$(dirname "$0")"
HERE="$PWD"
PY="$HOME/tof-env/bin/python"
[ -x "$PY" ] || { echo "!! $PY 없음 (ToF 가상환경)"; exit 1; }
echo "== 1/5 라이브러리"
"$PY" -c "import numpy, cv2, ArducamDepthCamera; print('numpy · cv2 · ArducamDepthCamera 있음')"
echo "== 2/5 예전 실행본 끄기 (센서를 동시에 열면 안 됨)"
rm -f "$HOME/.config/autostart/sensors.desktop"
pkill -f start_sensors.sh 2>/dev/null || true
pkill -f sensors_display.py 2>/dev/null || true
pkill -f tof-web-viewer/server.py 2>/dev/null || true
bash stop.sh >/dev/null 2>&1 || true
echo "== 3/5 직렬 포트 (GPIO 8·10번 핀)"
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
grep -q '^enable_uart=1' "$CFG" || echo 'enable_uart=1' | sudo tee -a "$CFG" >/dev/null
sudo raspi-config nonint do_serial_cons 1 || true
sudo usermod -aG dialout,video "$USER"
echo "== 4/5 수집단 서비스 (ripples-collector)"
mkdir -p run
sudo tee /etc/systemd/system/ripples-collector.service >/dev/null <<UNIT
[Unit]
Description=Ripples 수집단 (ToF + 천장 거리 → 저장 + 실시간 전달)
After=local-fs.target time-sync.target

[Service]
User=$USER
WorkingDirectory=$HERE
ExecStart=$PY $HERE/collector/collector.py
Restart=always
RestartSec=3
Nice=-5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable ripples-collector >/dev/null
sudo systemctl restart ripples-collector
sleep 6
"$PY" collector/status.py || true
echo "== 5/5 표시단 자동 시작 (전시 모드)"
chmod +x ./*.sh
if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  echo "!! Chromium 없음 · 혈 화면을 띄울 브라우저가 필요해요 (인터넷 되는 곳에서 sudo apt install -y chromium)"
fi
bash exhibition.sh on
echo
df -h "$HOME" | tail -1 | awk '{print "SD 카드: 전체 "$2" · 사용 "$3" · 남음 "$4}'
echo "끝. 재부팅하면 수집단(부팅 즉시)과 혈 화면(데스크톱 로그인 후)이 자동으로 켜져요: sudo reboot"
