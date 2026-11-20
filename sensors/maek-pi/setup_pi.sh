#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ "$(uname -s)" != Linux ]]; then
  echo 'Run this installer on the Raspberry Pi.' >&2
  exit 1
fi
sudo apt update
sudo apt install -y python3 python3-numpy python3-serial alsa-utils
sudo /usr/bin/python3 tools/enable_i2s.py
printf '\n초기 설치 완료. 배선 확인 후 sudo reboot. 이후 /usr/bin/python3 run.py\n'
