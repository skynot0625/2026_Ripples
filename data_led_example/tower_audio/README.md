# 심탑 소리 예시 — Active 20분 / Sleep 10분

Active와 환경음은 코드로 합성했습니다. 목소리는 **Windows 기본 한국어 TTS(Microsoft Heami Desktop)**로 만든 예시이며, 실제 현장 녹음이나 특정인의 복제 음성이 아닙니다. [샘플별 출처·원본 구간·처리 효과](SOURCES.md)를 명시했습니다. 실제 맥 녹음에는 같은 추출·변형 코드를 적용합니다. 현재 말소리 후보는 재생 계획에서 제외됩니다.

| 용도 | Active | Sleep |
|---|---|---|
| 먼저 듣기 · 90초 | [MP3](activate_90s_preview.mp3) | [MP3](sleep_90s_preview.mp3) |
| 전체 · 스테레오 | [20분 MP3](activate_20min_stereo.mp3) | [10분 MP3](sleep_10min_stereo.mp3) |
| 실제 네 출력용 분리 채널 | [20분 4ch FLAC](activate_20min_4ch.flac) | [10분 4ch FLAC](sleep_10min_4ch.flac) |

파일에는 master **0.26**을 적용했습니다. Active는 143개 이벤트(36/36/35/36), Sleep은 82개(20/21/20/21)이며 잔향까지 포함해 최대 두 소리입니다. Active는 부드러운 기계 동작·느린 코러스·2.4초 확산 잔향의 `dream-work-v3`입니다. **현재 두 상태 모두 말소리 없이 재생**합니다. MP3는 작은 인코더 패딩이 있을 수 있고 FLAC은 정확히 1,200초/600초입니다.

[전환 큐 두 방향 · 12초](transitions/sleep_then_activate.wav): Sleep 진입 5초, 1초 여백, Activate 진입 5초, 1초 여백. 베지어 LED 전환과 같은 곡선으로 합성했습니다. 기존 [목소리 효과 비교](voice_dry_then_dream.wav)는 이전 출처·연구 예시로 보관하며 현재 재생 목록에는 들어가지 않습니다.

![출력별 20분·10분 배치](speaker_timeline.png)

## 데이터 폼

- `synthetic_source/audio.wav`: 48초, 44.1kHz, PCM16, 3ch. `metadata.json`에 `demo:true`와 SHA256.
- `synthetic_source/voice.wav`: 약 18초, 44.1kHz PCM16 3ch 형태로 배치한 합성 음성. `voice.json`에 엔진·음성명·문안·SHA256. 실제 마이크 세 개로 녹음한 것이 아닙니다.
- [manifest.json](../../exhibition-server/static/tower-audio/manifest.json): `ripples.tower-audio.v1`, 음량 정책, 시간표, 뱅크, 두 상태의 이벤트 계획.
- `bank[]`: `id`, `state`, `file`, `duration`(초), `source`. Sleep source에는 선택 마이크(1부터), 원본 sample rate, 각 원본 조각의 `file_frame/session_frame/frames`, 점수와 변형 배율이 있습니다.
- `plans.activate[] / plans.sleep[]`: `{id, start, duration, speaker, gain}`. 시간은 상태 시작부터 초, speaker는 **0–3**, gain은 master 이전 배율입니다. 잔향은 duration 안에 포함합니다.
- [bank WAV](../../exhibition-server/static/tower-audio/bank): 24kHz PCM16 모노. 실제 네 출력은 이벤트의 speaker로 정합니다.
- [validation.json](validation.json): 출력별 peak/RMS, 동시 발성 수, 파일 크기, 정확한 원본 길이.

전시 컴퓨터의 [공간 3D](http://127.0.0.1:8000/venue3d?test)에서도 상태별로 들을 수 있습니다. 이 주소의 `127.0.0.1`은 그 페이지를 여는 컴퓨터 자신을 뜻합니다.

[소리 설계·원음 연결·출력·용량 설명](../../docs/TOWER_AUDIO.md)
