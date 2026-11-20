# 혈 Pi 설정 — 고친 뒤 「혈 끄기」 → 「혈 시작」 (또는 bash stop.sh; bash start.sh)
# 카메라 번호. 안 열리면 8
CAMERA_INDEX=0
# ToF 카메라를 거꾸로 달았으면 180 (화면에 쓰는 그림만 돌림, 저장 원본은 그대로)
TOF_ROTATE=180
# 천장 거리 센서 포트: auto (USB 어댑터 → GPIO 8·10번 핀) / /dev/ttyAMA0 (GPIO 핀) / /dev/ttyUSB0 (USB 어댑터)
DISTANCE_PORT=auto
# 센서 속도: 0 = 자동 (921600 공장값부터)
DISTANCE_BAUD=0
# 화면 오른쪽 아래 점검 칸 (fps · 카메라 · 천장 거리 · 혈 움직임 · Pi 온도/전원): 1 = 켜고 시작, 0 = 끄고 시작.
# 화면에서 키보드 D 로 언제든 켜고 끔. bash exhibition.sh on 하면 0 으로 바뀜
TEST_PANEL=0

# ---- 데이터 수집 (수집단 collector · systemd 서비스 ripples-collector) ----------------------------------
# 고친 뒤: sudo systemctl restart ripples-collector
# 저장 폴더 (날짜별 하위 폴더)
DATA_DIR=$HOME/ripples-data
# 1 = 저장, 0 = 저장 안 하고 실시간 전달만
RECORD=0
# ToF 프레임(깊이맵 전체) 저장: all = 들어오는 프레임 전부 (~30 fps, raw 전체), 숫자 = 1초에 그 장수, 0 = 저장 안 함
# (요약 통계 CSV · 천장 거리 50 Hz 는 항상 전부 저장)
TOF_SAVE_FPS=5
# ToF 파일 하나에 몇 초씩 묶을지 / 압축 수준 (1 = 빠름, 9 = 작음. Pi 에서 30 fps 를 따라가려면 1~3)
TOF_CHUNK_SECONDS=10
TOF_COMPRESS=1
# DATA_DIR 최대 크기 (GB). 넘으면 오래된 파일부터 지움
DATA_BUDGET_GB=14
# SD 카드에 항상 남겨둘 공간 (GB). 이보다 적으면 오래된 것부터 지우고, 그래도 부족하면 ToF 프레임 저장만 멈춤
MIN_FREE_GB=3

# 혈 움직임 추정 끄기 (공간선이 카메라 기준일 때는 쓰지 않음, Pi 4 부담 줄임). 켜려면 이 줄을 지움
NO_ODOMETRY=1
# 화면 쪽이 받는 ToF 수 (0 = 전부 30). Pi 4는 15 권장 (저장 원본은 그대로)
TOF_FPS=10
