# 음원 출처 명세

이 예시는 외부 음원 사이트의 녹음 샘플을 사용하지 않았습니다. Active는 코드 합성, Sleep 환경음도 코드 합성 원음입니다. 목소리는 **Windows 기본 한국어 TTS**로 만든 예시로 실제 관객 녹음·특정인의 복제 음성이 아닙니다. 현재 Active/Sleep 재생 계획에서는 말소리 후보를 제외했습니다. 아래 음성 항목은 출처를 보존한 이전 예시입니다.

효과를 적용한 WAV의 전체 SHA256과 원본 구간별 SHA256·프레임은 [manifest.json](../../exhibition-server/static/tower-audio/manifest.json)에 기록합니다. 아래 발췌 시간은 원본 파일의 초 단위입니다.

| 샘플 | 출처 | 원본 / 구간 | 처리 |
|---|---|---|---|
| [servo_breath](../../exhibition-server/static/tower-audio/bank/servo_breath.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [soft_relay](../../exhibition-server/static/tower-audio/bank/soft_relay.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [calculation](../../exhibition-server/static/tower-audio/bank/calculation.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [air_valve](../../exhibition-server/static/tower-audio/bank/air_valve.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [metabolic_pulse](../../exhibition-server/static/tower-audio/bank/metabolic_pulse.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [telemetry](../../exhibition-server/static/tower-audio/bank/telemetry.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [paper_motor](../../exhibition-server/static/tower-audio/bank/paper_motor.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [liquid_circuit](../../exhibition-server/static/tower-audio/bank/liquid_circuit.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [rotor_rest](../../exhibition-server/static/tower-audio/bank/rotor_rest.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [memory_keys](../../exhibition-server/static/tower-audio/bank/memory_keys.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [orbital_hum](../../exhibition-server/static/tower-audio/bank/orbital_hum.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [tiny_gears](../../exhibition-server/static/tower-audio/bank/tiny_gears.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [keyboard_mechanical](../../exhibition-server/static/tower-audio/bank/keyboard_mechanical.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [keyboard_spacebar](../../exhibition-server/static/tower-audio/bank/keyboard_spacebar.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [scanner_steps](../../exhibition-server/static/tower-audio/bank/scanner_steps.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [soft_latch](../../exhibition-server/static/tower-audio/bank/soft_latch.wav) | 직접 코드 합성 | active_bank · seed 214 · dream-work-v3 | soft mechanical synthesis; rounded attacks, low-pass, gentle chorus and 2.4s diffuse reverb tail; no speech |
| [sleep_voice_00](../../exhibition-server/static/tower-audio/bank/sleep_voice_00.wav) | Microsoft Heami Desktop / Windows SAPI 합성 음성 | voice.wav · MIC 2 · 0.49–5.63s | 현재 재생 제외 · 보관한 이전 음성 예시; 음높이 유지 · 0.96배 속도 · 작은 2.4초 잔향 |
| [sleep_voice_01](../../exhibition-server/static/tower-audio/bank/sleep_voice_01.wav) | Microsoft Heami Desktop / Windows SAPI 합성 음성 | voice.wav · MIC 2 · 11.15–16.65s | 현재 재생 제외 · 보관한 이전 음성 예시; 음높이 유지 · 0.96배 속도 · 작은 2.4초 잔향 |
| [sleep_voice_02](../../exhibition-server/static/tower-audio/bank/sleep_voice_02.wav) | Microsoft Heami Desktop / Windows SAPI 합성 음성 | voice.wav · MIC 2 · 5.85–10.93s | 현재 재생 제외 · 보관한 이전 음성 예시; 음높이 유지 · 0.96배 속도 · 작은 2.4초 잔향 |
| [sleep_03](../../exhibition-server/static/tower-audio/bank/sleep_03.wav) | 합성 맥 환경음 | audio.wav · MIC 2 · 15.50–18.70s | slow playback + 65–2600 Hz + quiet diffuse echoes |
| [sleep_04](../../exhibition-server/static/tower-audio/bank/sleep_04.wav) | 합성 맥 환경음 | audio.wav · MIC 3 · 29.50–32.70s | slow playback + 65–2600 Hz + quiet diffuse echoes |
| [sleep_05](../../exhibition-server/static/tower-audio/bank/sleep_05.wav) | 합성 맥 환경음 | audio.wav · MIC 1 · 1.00–4.20s | slow playback + 65–2600 Hz + quiet diffuse echoes |
| [sleep_06](../../exhibition-server/static/tower-audio/bank/sleep_06.wav) | 합성 맥 환경음 | audio.wav · MIC 2 · 4.50–7.70s | slow playback + 65–2600 Hz + quiet diffuse echoes |
| [sleep_07](../../exhibition-server/static/tower-audio/bank/sleep_07.wav) | 합성 맥 환경음 | audio.wav · MIC 1 · 34.00–37.20s | slow playback + 65–2600 Hz + quiet diffuse echoes |
| [sleep_08](../../exhibition-server/static/tower-audio/bank/sleep_08.wav) | 합성 맥 환경음 | audio.wav · MIC 3 · 19.50–22.70s | slow playback + 65–2600 Hz + quiet diffuse echoes |
| [sleep_09](../../exhibition-server/static/tower-audio/bank/sleep_09.wav) | 합성 맥 환경음 | audio.wav · MIC 3 · 8.00–11.20s | slow playback + 65–2600 Hz + quiet diffuse echoes |
| [sleep_10](../../exhibition-server/static/tower-audio/bank/sleep_10.wav) | 합성 맥 환경음 | audio.wav · MIC 2 · 38.00–41.20s | slow playback + 65–2600 Hz + quiet diffuse echoes |
| [sleep_11](../../exhibition-server/static/tower-audio/bank/sleep_11.wav) | 합성 맥 환경음 | audio.wav · MIC 1 · 23.00–24.00s | slow playback + 65–2600 Hz + quiet diffuse echoes |

## 목소리 예시의 원본

[voice.wav](synthetic_source/voice.wav) / [생성 정보와 문안](synthetic_source/voice.json). Microsoft Windows의 설치된 `Microsoft Heami Desktop` 음성, System.Speech/SAPI, Rate −1을 사용했습니다. 이 프로젝트 예시를 위해 작성한 문안이며 현장에서 들은 발화가 아닙니다.

> 잠시 걸음을 멈추고, 이 공간의 소리를 들어보세요.
> 지나간 작은 소리들이, 천천히 기억 속에 머뭅니다.
> 우리는 같은 공간에서, 서로 다른 소리를 듣고 있습니다.

[원음 → 효과 비교](voice_dry_then_dream.wav)는 첫 문장의 마른 소리, 1초 여백, 같은 소리의 Sleep 효과 순서입니다. 비교용으로 두 버전의 음량을 낮춰 맞췄습니다. 원본 TTS 소스 파일 자체는 출력용 음량을 적용하기 전입니다.

## 최종 재생 파일

20분/10분 파일과 90초 발췌는 위 샘플을 manifest의 `plans`대로 섞은 것입니다. 4채널 FLAC은 독립 출력, MP3는 스테레오 합본입니다. 원음·발췌·효과·최종 믹스를 구분하여 추적할 수 있습니다.

## 코드와 도구의 출처

- 작업음과 환경음 생성: 이 저장소의 `src/ripples/tower_audio.py`.
- 목소리 구간 검출: [WebRTC VAD Python 구현](https://github.com/daanzu/py-webrtcvad-wheels), `webrtcvad-wheels 2.0.14.post1`. 음원 제공자가 아니라 검출 도구입니다. 문장 내용·화자 신원을 인식하지 않습니다.
- 자연스러운 속도 조절: [FFmpeg atempo](https://ffmpeg.org/ffmpeg-filters.html#atempo). 음정 이동·역재생·음성 복제는 적용하지 않습니다.
- 실제 맥 원음이 들어오면 `recording` 출처로 별도 기록합니다. 원본의 장치·세션·마이크·프레임·해시를 유지하며 자동 자막이나 추정 화자 이름을 만들지 않습니다.

합성기·라이브러리·Windows 음성의 출처 표시는 해당 도구 자체를 이 프로젝트가 제작했다는 뜻이 아닙니다.

## 상태 전환 큐

`src/ripples/transition_audio.py::make_cue`가 직접 합성합니다. 외부 음원·녹음·TTS를 사용하지 않습니다. 기존 LED `pulse_field`의 5초 상승·감쇠 곡선과 부드러운 배음으로 만들며, [큐 파일과 SHA256](../../exhibition-server/static/tower-audio/cues.json)에 출처를 기록합니다. [두 방향 미리듣기](transitions/sleep_then_activate.wav)는 Sleep 진입, 1초 여백, Activate 진입 순서입니다.
