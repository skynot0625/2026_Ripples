#!/bin/bash
# 맥에서: Pi 에 쌓인 데이터(~/ripples-data)를 맥으로 가져온다. 이미 받은 파일은 건너뜀.
#   bash pull_data.sh [저장할 폴더=~/ripples-data] [ripples@10.42.0.1]
set -e
DEST=${1:-$HOME/ripples-data}; PI=${2:-ripples@10.42.0.1}
mkdir -p "$DEST"
ssh "$PI" "du -sh ~/ripples-data; df -h / | tail -1"
# 쓰는 중인 .csv / .tmp 도 같이 오지만 다음 실행 때 완성본으로 다시 받아짐
rsync -av --partial --exclude '*.tmp' "$PI:ripples-data/" "$DEST/"
echo "완료 → $DEST   (Pi 쪽은 용량 한도에 따라 오래된 것부터 자동 삭제됨)"
