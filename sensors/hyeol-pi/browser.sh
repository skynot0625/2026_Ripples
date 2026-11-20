#!/bin/bash
# 혈 화면 브라우저만 다시 띄움 (번역 제안 끔)
cd ~/ripples-hyeol-pi
export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 DISPLAY=:0
[ -f run/browser.pid ] && kill "$(cat run/browser.pid)" 2>/dev/null
for p in $(pgrep -x chromium); do kill $p 2>/dev/null; done; sleep 3
python3 - <<'PY'
import json, os
p = "run/chromium/Default/Preferences"
if os.path.exists(p):
    d = json.load(open(p)); d.setdefault("translate", {})["enabled"] = False
    d["translate_blocked_languages"] = ["ko", "en"]; d.setdefault("intl", {})["accept_languages"] = "ko,en"
    json.dump(d, open(p, "w")); print("번역 꺼짐 설정")
PY
. ./settings.sh
setsid -f chromium --kiosk --noerrdialogs --disable-infobars --no-first-run --password-store=basic --disable-session-crashed-bubble \
  --disable-features=Translate,TranslateUI --lang=ko --user-data-dir="$PWD/run/chromium" \
  "http://127.0.0.1:8000/output/hyeol?clean&panel=${TEST_PANEL:-0}" >> run/browser.log 2>&1 < /dev/null
sleep 3; pgrep -x chromium | head -1 > run/browser.pid
