#!/bin/bash
# 혈 Pi 기구 화면 서버만 다시 켜기 (2026-11-20). 수집단 · 송신 · 브라우저는 그대로
cd /home/ripples/ripples-hyeol-pi || exit 1
pkill -f "[t]of-env/bin/python app/server.py" ; sleep 2
setsid nohup /home/ripples/tof-env/bin/python app/server.py --standalone --host 127.0.0.1 --port 8000 --source collector --distance collector --no-odometry \
  --visual-file /home/ripples/ripples-hyeol-pi/data/visual.json --mapping-file /home/ripples/ripples-hyeol-pi/data/mapping.json \
  --follow "$(head -1 follow.txt)" >> run/server.log 2>&1 < /dev/null &
