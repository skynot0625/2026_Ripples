#!/bin/bash
# 맥 Pi 지지직 수정판 시험 — 인터넷 없이 Pi 핫스팟에만 붙은 맥북에서 한 번에 돌린다.
# 수정판 zip 을 Pi 에 올려 실제 마이크 40초 · 가짜 입력(--demo) 10초를 재생하고,
# 상태 줄 기록을 맥으로 가져온다. 인터넷으로 돌아온 뒤 기록 폴더를 AI 에게 보여 주면 된다.
#
#   bash pi_test_from_mac.sh            (기본 mic@mic.local)
#   bash pi_test_from_mac.sh mic@다른주소
#
# 비밀번호는 처음 한 번만 묻는다(ssh 연결을 재사용).
set -u
HOST="${1:-mic@mic.local}"
RIPPLES="${RIPPLES:-$HOME/ripples}"   # 리플즈 작업 폴더
ZIP="$RIPPLES/outputs/maek-teensy-speaker_declick.zip"
STAMP="$(date +%Y%m%d-%H%M)"
LOCAL_LOG="$RIPPLES/work/maek-pi-test-$STAMP"
SOCK="/tmp/maek-ssh-$$"
SSH=(ssh -o ControlMaster=auto -o ControlPath="$SOCK" -o ControlPersist=600 -o ConnectTimeout=8)

cleanup() { ssh -o ControlPath="$SOCK" -O exit "$HOST" 2>/dev/null; }
trap cleanup EXIT

echo "== 1/5 Pi 접속 ($HOST) — 비밀번호를 물으면 입력"
"${SSH[@]}" "$HOST" true || { echo "접속 실패: 맥북이 Pi 핫스팟에 붙었는지 확인하세요."; exit 1; }

echo "== 2/5 수정판 올리기 → ~/maek-declick (기존 폴더는 건드리지 않음)"
scp -o ControlPath="$SOCK" -q "$ZIP" "$HOST:~/maek-teensy-speaker_declick.zip" || exit 1
"${SSH[@]}" "$HOST" 'unzip -oq ~/maek-teensy-speaker_declick.zip -d ~/maek-declick && mkdir -p ~/maek-declick/logs' || exit 1

RUNNING="$("${SSH[@]}" "$HOST" "pgrep -af 'python[0-9.]* .*(run\.py|monitor|record|wav_upload)'" || true)"
if [ -n "$RUNNING" ]; then
  echo "!! Pi 에서 이미 도는 프로그램이 있어요 (USB 포트를 같이 못 씀):"
  echo "$RUNNING"
  read -r -p "   이것들을 끄고 계속할까요? [y/N] " answer
  [ "$answer" = "y" ] || exit 1
  "${SSH[@]}" "$HOST" "pkill -f 'python[0-9.]* .*(run\.py|monitor|record|wav_upload)'; sleep 1"
fi

APP='cd ~/maek-declick/maek-teensy-speaker'
echo "== 3/5 실제 마이크 40초 재생 — 스피커 소리를 들어 보세요"
"${SSH[@]}" -t "$HOST" "$APP && /usr/bin/python3 run.py --seconds 40 2>&1 | tee ~/maek-declick/logs/live-$STAMP.log"

echo "== 4/5 가짜 입력(--demo) 10초 — 왼쪽 440Hz · 오른쪽 880Hz 삐 소리"
"${SSH[@]}" -t "$HOST" "$APP && /usr/bin/python3 run.py --demo --seconds 10 2>&1 | tee ~/maek-declick/logs/demo-$STAMP.log"

echo "== 5/5 기록 가져오기"
mkdir -p "$LOCAL_LOG"
scp -o ControlPath="$SOCK" -q "$HOST:~/maek-declick/logs/*-$STAMP.log" "$LOCAL_LOG/"
"${SSH[@]}" "$HOST" 'aplay -l; uptime; vcgencmd get_throttled 2>/dev/null' > "$LOCAL_LOG/pi-info.txt" 2>&1
echo
echo "끝. 기록: $LOCAL_LOG"
echo "들은 느낌(실제 마이크 · demo 각각 지지직 있었는지)을 메모해 두고, 인터넷에 다시 붙은 뒤 이 폴더를 알려 주세요."
