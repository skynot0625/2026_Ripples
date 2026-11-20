# 심 LED 탑·센서 예시 자료

이 디렉터리는 현재 설치를 설명하는 참조 자료와 재현 가능한 작은 입력 예시다.
실제 제어 코드는 `src/ripples/hardware`, 실행 결과는 Git에서 제외한 `runtime`/`outputs`에 있다.

| 폴더 | 내용·출처 |
|---|---|
| [m30_session](m30_session/README.md) | 실제 M30 배치·오른쪽 전시실 맵핑 + 명시적인 합성 혈/맥/RSSI 폼 4,800개, schema·raw 샘플·해시 |
| layout | 2026-11-12 LED 인계본의 배치/포트/길이/34개 색 보정, 배치·배선 PNG |
| hardware_reference | 제공받은 하드웨어·설치 문서 원문. 문서 내 옛 경로는 원래 인계본 구조 |
| firmware_reference | 설치된 LED2 65슬롯 펌웨어 소스·바이너리·빌드 기록·FastLED 패치/라이선스 |
| sensors | 제공된 실제 혈 59초 ZIP, 비콘 RSSI 예시와 BLE 규격, 맥 WAV 규격 |
| audio_synthetic_sample | 명시적으로 합성한 3채널 WAV 1초와 demo=true metadata |
| spikes | N=8,460, gain=9.5, seed=0, dt=0.1ms, 60,000스텝의 실제 reservoir 이벤트 |

`spikes/example_spikes.npz`는 뉴런을 새로 만들지 않고 동일 발화로 시각 효과를 비교하기 위한
6초 모델 기록이다. 40스텝/프레임, 10FPS 표시에서는 150초 분량이다.

전체 합성 맥 240초와 DB는 저장소에 포함하지 않는다. `python -m ripples.demo`로 로컬 생성한다.
`audio_synthetic_sample`은 기본 혈 입력 폴더 밖에 두어 실제 센서 기록처럼 자동 혼합되지 않게 했다.

펌웨어는 **참조용**이며 실행 시 자동 플래시하지 않는다. Arduino ESP32 core 3.3.12와
프로젝트 패치 FastLED 3.10.5 기반이다. 전체 vendor 트리/툴체인은 포함하지 않았으므로
완성된 오프라인 펌웨어 빌드 키트는 아니다. 현재 설치 사용은 이미 올라간 펌웨어를 전제로 한다.
FastLED 관련 원문 라이선스를 함께 제공한다.

`MANIFEST_SHA256.json`은 제공한 바이너리·설정·샘플 파일의 크기와 SHA256 목록이다.
사용자 컴퓨터의 전체 경로나 수집 DB/로그는 포함하지 않는다.
