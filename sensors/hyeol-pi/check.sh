#!/bin/bash
cd "$(dirname "$0")"
. ./settings.sh
PY="$HOME/tof-env/bin/python"; [ -x "$PY" ] || PY=python3
# 수집단 (센서를 실제로 읽는 쪽)
"$PY" collector/status.py
if [ -f run/server.pid ] && kill -0 "$(cat run/server.pid)" 2>/dev/null; then
  # 켜져 있는 동안은 카메라 · 센서를 서버가 쓰고 있으니, 서버에 물어본다
  "$PY" app/pi_check.py --running
elif systemctl is-active --quiet ripples-collector 2>/dev/null; then
  # 수집단이 카메라 · 직렬 포트를 쓰고 있으니 직접 열지 않고 설정 · 라이브러리만 확인
  "$PY" app/pi_check.py
else
  "$PY" app/pi_check.py --camera --camera-index "$CAMERA_INDEX" --distance "$DISTANCE_PORT" --distance-baud "$DISTANCE_BAUD"
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
