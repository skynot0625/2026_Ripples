#!/bin/bash
# 혈 Pi 기구 화면 서버만 다시 켜기 (2026-11-20). 수집단 · 송신 · 브라우저는 그대로. start.sh 와 같은 설정(settings.sh: TOF_ROTATE 등)으로 켠다
cd /home/ripples/ripples-hyeol-pi || exit 1
. ./settings.sh
export TOF_ROTATE="${TOF_ROTATE:-0}" TOF_FPS="${TOF_FPS:-0}"
pkill -f "[t]of-env/bin/python app/server.py" ; sleep 2
FOLLOW=$(grep -v '^#' follow.txt 2>/dev/null | head -1 | tr -d '[:space:]')
setsid nohup /home/ripples/tof-env/bin/python app/server.py --standalone --host 127.0.0.1 --port 8000 --source collector --distance collector ${NO_ODOMETRY:+--no-odometry} \
  --visual-file "$PWD/data/visual.json" --mapping-file "$PWD/data/mapping.json" ${FOLLOW:+--follow "$FOLLOW"} >> run/server.log 2>&1 < /dev/null &
echo $! > run/server.pid
