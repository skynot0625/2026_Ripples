# 문래 M30 · 심탑 현장 운영 기준

2026-11-25 기준. 설치 방법보다 실제 실행 경로와 설명의 기준을 기록합니다. 이전 날짜의 시험 기록은 [validation](validation.md)에 보존합니다.

## 운영 시계와 소스

- 시간표: Asia/Seoul, 10:30부터 20분 Active + 8분 경로 Sleep + 2분 전체 Sleep.
- 일일 운영 창: 10:30–19:00. Windows 작업 `ripples_sim_daily`가 시작하고 5분 간격 복구 트리거를 둡니다. 파일 잠금으로 중복 컨트롤러를 방지합니다.
- 실행 중 컨트롤러: 로컬 `runtime/sim_daily.py`. 공개 정리본은 [scripts/sim_daily.py](../scripts/sim_daily.py)이며, 로컬 전용 경로를 환경변수로 바꿨습니다. 작업 경로는 유지합니다. 이번 점검에서 오디오 초기화와 별도 재생 프로세스 운영을 로컬·공개본 모두에 반영하고 일일 작업을 재시작했습니다.
- 센서 하나 이상이 10초 이어 연결되면 live, 둘 다 30초 이상 끊기면 저장 예시로 전환합니다. 시작 시에는 현재 연결 여부로 선택합니다.
- **연결 여부와 위치 유효성은 다릅니다.** 음량만 들어와도 live를 선택할 수 있으나 신선한 비콘이 부족하면 센서 공간 표시는 없고 SNN만 보일 수 있습니다. 이를 관객의 관측 데이터가 가득 찬 장면처럼 설명하지 않습니다.
- USB 장애로 자식 프로세스가 종료되면 컨트롤러가 재시작합니다. 상태는 시계에서 다시 구합니다. 보드 전원·배선·센서 하드웨어 자체를 소프트웨어가 복구할 수 있다는 뜻은 아닙니다.
- `runtime/sim-daily.off`는 당일 자동 재시작도 막는 운영 중지 표식입니다.

## 표시와 수신

실시간 경로는 `ripples.live --accumulate`로 실행합니다. 센서 수신·저장은 Active/Sleep 양쪽에서 계속됩니다. 화면은 흰색 세 층이며 SNN은 두 상태 공통, Active의 발화 밝기만 12%입니다. 모델은 8460 뉴런/gain9.5/dt0.1ms/40스텝을 유지합니다.

Sleep 경로는 진입 시 고정하고 공간 통계는 계속 갱신될 수 있습니다. 마지막 2분의 전체 기억도 새로운 유효 관측이 들어오면 갱신됩니다. ‘모든 데이터 유지’는 표시와 이력 보존을 의미하며 통계 업데이트를 멈춘다는 뜻은 아닙니다.

| 데이터 | 파일 / 성격 |
|---|---|
| 오늘 일일 live | `runtime/exhibition-live-estimated.sqlite3` / 상대 RSSI, 실측 센서 |
| 별도 수신 아카이브 | `runtime/history-live-estimated.sqlite3` / 별도 프로세스의 실측 추정 기록 |
| 저장 LED 예시 | `runtime/m30-example-history.sqlite3` / 합성 예시 |
| 공간 3D | `runtime/venue3d-history.sqlite3` / 시뮬레이션 위치·합성 특징 |
| 실시간 상태 보고 | `runtime/live-status.json` / 상태·입력·위치·저장 오류·FPS |

DB 사이 자동 병합은 없습니다. 실제 데이터, 예시, 보정 조건을 섞지 않습니다. 공간 DB에는 원시 녹음·카메라 영상이 없고 1초 특징 통계와 경로가 있습니다. 40시간 경로를 보존하되 화면 응답만 최대 9,600점/기구로 줄입니다.

## 소리

사용자 제공 48kHz·스테레오·30분 MP3를 시계의 30분 위치에서 시작합니다. 앞 20분 Active, 뒤 10분 Sleep입니다. 네 채널에는 L,R,L,R을 보냅니다. 따라서 음악 자체가 네 독립 트랙인 것은 아닙니다. 현장 컨트롤러는 전환 큐를 추가하고, 저장 예시 플레이어는 기존 MP3 재생을 사용합니다.

**Realtek Quadraphonic 설정·채널 순서를 바꾸지 않습니다.** 1 FL 안쪽 / 2 FR 입구 / 3 RL 길가 창문 / 4 RR. 플레이어 gain 1.0, 앰프로 청취 음량을 조절합니다. [확정 설정](../configs/audio.site-quad.json). 기존 `audio.realtek.m30.json`은 이전 8채널 시험용 설정으로 표시해 현장 기본값과 구분합니다.

오디오는 별도 자식 프로세스에서 장치를 초기화하며 오류는 runtime/sim-audio.log에 기록합니다.

공개 스크립트는 `RIPPLES_MP3_PATH`로 로컬 파일을 지정합니다. MP3·관람자 기록·SQLite DB·시스템 로그는 Git에 포함하지 않습니다. 공개본의 환경변수 변경은 현재 실행 중인 로컬 재생기의 설정을 바꾸지 않습니다.

## 코드 경계와 재현성

| 경계 | 코드 |
|---|---|
| 현장 자동 운전 / 저장 예시 | `scripts/sim_daily.py`, `scripts/play_exhibition_example.py` |
| 센서 실시간 조립 / 별도 기록 | `src/ripples/live.py`, `archive.py`, `realtime.py` |
| 영구 경로·통계 / Sleep 마스크 | `memory.py`, `sleep_replay.py` |
| 흰색 3층·잔광·전환 | `visualization.py`, `transition.py` |
| 3D 예시와 영구 기억 API | `exhibition-server/static/ripples-example.js`, `history_control.py` |
| 비콘·ToF·마이크 입력 계약 | `docs/DATA_FLOW.md`, `docs/realtime.md` |

코드 공개는 실행 중 서버·브라우저 캐시가 모두 같은 버전으로 바뀌었다는 뜻은 아닙니다. 로컬 서버와 저장소 자산의 일치 여부는 검증 기록에 별도로 남깁니다. 3D는 USB의 실시간 미러가 아니라 같은 표상 규칙을 사용하는 별도 예시 경로입니다.
