#!/bin/bash
# 맥에서: Pi 에 이 폴더를 복사 → 설치(수집 서비스 + 시작 프로그램) → 재부팅.
#   bash deploy.sh                 # ripples@10.42.0.1, 끝나면 재부팅
#   bash deploy.sh --no-reboot     # 재부팅 없이 (수집 서비스만 바로 재시작됨)
#   bash deploy.sh ripples@다른IP
# 맥이 Pi Wi-Fi(ripples-direct)에 연결돼 있어야 함. 비밀번호를 여러 번 물으면: ssh-copy-id ripples@10.42.0.1 한 번.
set -e
cd "$(dirname "$0")"
PI=ripples@10.42.0.1; REBOOT=1
for a in "$@"; do case "$a" in --no-reboot) REBOOT=0;; *) PI=$a;; esac; done

echo "== 연결 확인 $PI"
ssh -o ConnectTimeout=5 "$PI" true || { echo "Pi 에 연결 안 됨 — 맥 Wi-Fi 가 ripples-direct 인지 확인"; exit 1; }
echo "== 복사 → $PI:~/ripples-hyeol-pi  (run/, _backup*, .DS_Store 제외)"
COPYFILE_DISABLE=1 tar czf - --exclude ./run --exclude './_backup*' --exclude .DS_Store . \
  | ssh "$PI" "mkdir -p ~/ripples-hyeol-pi && tar xzf - -C ~/ripples-hyeol-pi"
echo "== Pi 시계를 맥 시계에 맞추고 설치 (Pi 는 인터넷이 없어 시계가 틀림 → 저장 파일 날짜)"
ssh -t "$PI" "sudo date -s @$(date +%s) >/dev/null && cd ~/ripples-hyeol-pi && bash setup_pi.sh"
if [ "$REBOOT" = 1 ]; then
  echo "== 재부팅 (약 1분 뒤 모니터에 화면이 뜸, 이후 확인: ssh -t $PI 'cd ~/ripples-hyeol-pi && bash check.sh')"
  ssh "$PI" "sudo systemctl reboot" || true
else
  echo "== 재부팅 안 함. 화면까지 새로 띄우려면: ssh $PI sudo reboot"
fi
