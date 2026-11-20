#!/bin/bash
# ripples BLE 비콘 수신기 설치 (혈 Pi · 맥 Pi 공용, 인터넷 없이). 이 폴더를 Pi 에 통째로 올린 뒤:
#   sudo bash install.sh            → 서비스 ripples-beacon (부팅 때 자동 시작, 죽으면 다시 켬)
# 되돌리기: sudo systemctl disable --now ripples-beacon && sudo rm /etc/systemd/system/ripples-beacon.service
set -e
DIR=$(cd "$(dirname "$0")" && pwd); U=${SUDO_USER:-$(whoami)}
PY=$(command -v python3); V=$("$PY" -c 'import sys;print(f"cp{sys.version_info[0]}{sys.version_info[1]}")')
echo "== 파이썬 $("$PY" -V) ($V) · 사용자 $U · 폴더 $DIR"
rm -rf "$DIR/vendor"; mkdir -p "$DIR/vendor"
for w in "$DIR"/wheels/bleak-*.whl "$DIR"/wheels/typing_extensions-*.whl; do "$PY" -m zipfile -e "$w" "$DIR/vendor"; done
W=$(ls "$DIR"/wheels/dbus_fast-*-"$V"-*.whl 2>/dev/null | head -1)
[ -n "$W" ] || { W=$(ls "$DIR"/wheels/dbus_fast-*.whl | head -1); echo "   (이 파이썬용 dbus_fast 가 없어 순수 파이썬으로 씀 — 조금 느림)"; }
"$PY" -m zipfile -e "$W" "$DIR/vendor"
PYTHONPATH="$DIR/vendor" "$PY" -c "import bleak, dbus_fast; print('   bleak', bleak.__version__ if hasattr(bleak,'__version__') else 'ok')"
"$PY" "$DIR/beacon_scanner.py" --selftest | head -1
mkdir -p "$DIR/run"; [ -f "$DIR/run/beacon.json" ] || cp "$DIR/beacon.example.json" "$DIR/run/beacon.json"
chown -R "$U": "$DIR"
rfkill unblock bluetooth 2>/dev/null || true
systemctl enable --now bluetooth >/dev/null 2>&1 || true
usermod -aG bluetooth "$U" 2>/dev/null || true
cat > /etc/systemd/system/ripples-beacon.service <<UNIT
[Unit]
Description=ripples BLE beacon scanner
After=bluetooth.service
Wants=bluetooth.service
[Service]
User=$U
WorkingDirectory=$DIR
Environment=PYTHONPATH=$DIR/vendor
ExecStart=$PY $DIR/beacon_scanner.py
Restart=always
RestartSec=3
Nice=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload; systemctl enable ripples-beacon >/dev/null 2>&1; systemctl restart ripples-beacon
sleep 6; echo "== 상태: $(systemctl is-active ripples-beacon)"; journalctl -u ripples-beacon -n 6 --no-pager | cut -c1-160
echo "== 확인: journalctl -u ripples-beacon -f   ·   cat $DIR/run/beacon_latest.json"
