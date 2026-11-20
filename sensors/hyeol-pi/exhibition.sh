#!/bin/bash
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
