#!/bin/bash
# 서버(실제 ToF · 천장 거리 센서, 시뮬레이터 없음)를 켜고 혈 화면을 전체 화면으로 띄운다.
cd "$(dirname "$0")"
. ./settings.sh
export TOF_ROTATE="${TOF_ROTATE:-0}"   # 카메라 방향 (0 / 180)
export TOF_FPS="${TOF_FPS:-0}"         # 화면 쪽이 받는 ToF 수 (0 = 전부)
mkdir -p run
# 파이썬: ToF 라이브러리가 깔린 가상환경 (없으면 시스템 python3)
PY="$HOME/tof-env/bin/python"; [ -x "$PY" ] || PY=python3
# 센서는 수집단(ripples-collector 서비스)이 읽는다. 꺼져 있으면 켠다.
if command -v systemctl >/dev/null && ! systemctl is-active --quiet ripples-collector; then
  sudo -n systemctl start ripples-collector 2>/dev/null || echo "수집단(ripples-collector)이 꺼져 있음 · sudo systemctl start ripples-collector"
fi
"$PY" app/pi_check.py || exit 1
if [ -f run/server.pid ] && kill -0 "$(cat run/server.pid)" 2>/dev/null; then
  echo "서버가 이미 켜져 있음 (bash stop.sh 로 끄고 다시)"; exit 1
fi
# 「Pi로 보내기」를 따라갈 전시컴 주소 (follow.txt 첫 줄, 비우면 따라가지 않음)
FOLLOW=$(grep -v '^#' follow.txt 2>/dev/null | head -1 | tr -d '[:space:]')
"$PY" app/server.py --standalone --host 127.0.0.1 --port 8000 --source collector --distance collector ${NO_ODOMETRY:+--no-odometry} \
  --visual-file "$PWD/data/visual.json" --mapping-file "$PWD/data/mapping.json" ${FOLLOW:+--follow "$FOLLOW"} >> run/server.log 2>&1 &
echo $! > run/server.pid
# 심 서버로 보내기 (app/sender.py, 설정 run/send.json — host 가 비면 보내지 않음)
[ -f run/send.json ] || echo '{"host": "", "port": 9500, "tof_hz": 10, "lidar_hz": 50}' > run/send.json
if ! { [ -f run/sender.pid ] && kill -0 "$(cat run/sender.pid)" 2>/dev/null; }; then
  "$PY" app/sender.py >> run/sender.log 2>&1 &
  echo $! > run/sender.pid
fi
for i in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:8000/status && break; sleep 0.25; done
curl -s -o /dev/null http://127.0.0.1:8000/status || { echo "서버 시작 실패 · run/server.log 확인"; exit 1; }
BROWSER=$(command -v chromium || command -v chromium-browser)
# --disable-features=Translate --lang=ko: 화면 오른쪽 위 「Korean · English」 번역 제안을 띄우지 않음
"$BROWSER" --kiosk --noerrdialogs --disable-infobars --no-first-run --password-store=basic --disable-session-crashed-bubble \
  --disable-features=Translate,TranslateUI --lang=ko \
  --user-data-dir="$PWD/run/chromium" "http://127.0.0.1:8000/output/hyeol?clean&panel=${TEST_PANEL:-0}" >> run/browser.log 2>&1 &
echo $! > run/browser.pid
sleep 3
"$PY" app/pi_check.py --running --quiet
echo "시작함 · 끄기: bash stop.sh (또는 바탕화면 「혈 끄기」)"
