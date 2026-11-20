# 맥 3마이크 PCM → 스피커·진동 출력

**상태: 함수 묶음 구현·합성 입력 검증. 실물 Pi·마이크·앰프·모터 연결 전. 기기 값은 설정 파일([config/maek_audio.ini](config/maek_audio.ini))로 넣는다.**

맥의 마이크 좌·중앙·우에서 **같은 시간 기준으로 받은 3채널 PCM**을 처리한다. 결과는 좌우 스피커용 2채널 PCM과 좌우 모터의 0–1 강도값이다. 원본 3채널 PCM은 별도 `PcmWavChunker`가 1초 WAV로 묶는다. 이 묶음에는 센서 캡처, ALSA/PipeWire, I²S 설정, 오디오 재생, PWM, 심 서버 전송이 없다.

## 확인한 것 / 가정 / 아직 모르는 것

| 상태 | 내용 |
|---|---|
| 확인 | 이전 설계의 세 출력은 원본 기록·전송, 방향→진동, 좌우 마이크→스테레오다. 중앙 마이크는 스테레오 재생에 섞지 않는다. |
| 확인 | 코드의 합성 PCM 검사와 1초 WAV 검사는 통과했다. 실제 기기에서 검증한 결과가 아니다. |
| 가정 | 내부 입력은 `float32` 또는 `float64`의 `(프레임 수, 3)` 배열, 값 범위 `-1…1`이다. 실제 PCM 형식에 맞는 캡처 어댑터가 필요하다. |
| 가정 | 필터 대역, 스피커 gain/최대 피크, 진동 감도·반응 속도 기본값은 기능 확인용이다. 실제 공간·스피커·모터에서 조정해야 한다. |
| 미결 | 실제 I²S 3채널 취득 방식, 3채널 동기화, 채널 순서, 샘플레이트/비트 정렬, 마이크 좌표, PCM 타임스탬프. |
| 미결 | 스피커·모터 드라이버의 입력 형식, 심 서버의 업로드 규칙, 허용 지연·음량, AEC 적용 방법. |

## 설치할 때 채우는 설정 파일

기술팀이 확인한 기기 값은 코드를 고치지 않고 [config/maek_audio.ini](config/maek_audio.ini)에 적는다. 파일 안의 설명을 따라 빈 칸을 채운다.

| 구역 | 적는 것 | 비우면 |
|---|---|---|
| `[input]` | 샘플레이트, 샘플 형식(`s16le` · `s24le` · `s24_32le` · `s32le` · `float32le`), 채널 순서, 블록 크기, 동기화 여부 | 샘플레이트·형식은 **필수**. 블록은 10ms |
| `[mic_positions_m]` | 세 마이크 좌표(m) | 방향 각도 끔 |
| `[speaker]` | 스피커 PCM 형식·샘플레이트 | 입력과 같게. 샘플레이트가 다르면 거절(리샘플링 없음) |
| `[motor]` | 드라이버 명령 범위(예: 0–255), 데드존, 갱신 주기 | 0–255 |
| `[server]` | 심 서버 주소·방법·기기 이름 | **기록만 한다. 전송 코드는 아직 없다** |
| `[tuning]` | 음량·대역·진동 감도 등 | 코드 기본값 |

```text
cp config/maek_audio.ini /etc/maek-audio/maek_audio.ini   # 위치는 자유
python -m maek_audio.check /etc/maek-audio/maek_audio.ini                       # 값 검사
python -m maek_audio.check /etc/maek-audio/maek_audio.ini --pcm 녹음.raw --out 확인.wav  # 실제 녹음으로 시험
```

빠지거나 틀린 값은 한 번에 목록으로 알려 주고 실행하지 않는다. 코드에서는 이렇게 쓴다.

```python
from maek_audio import load_settings

settings = load_settings("/etc/maek-audio/maek_audio.ini")  # 생략하면 $MAEK_AUDIO_CONFIG → ./maek_audio.ini
processor = settings.make_processor()
chunker = settings.make_chunker()
rate = settings.processor.sample_rate_hz

# raw_bytes: 캡처 장치에서 받은 블록 하나 (settings.input.frames_per_block 프레임)
result = processor.process(settings.decode_input(raw_bytes), sample_rate_hz=rate)
speaker_bytes = settings.encode_speaker(result.stereo_pcm)   # 스피커 장치로
left_cmd, right_cmd = settings.motor_commands(result)        # 모터 드라이버로
for chunk in chunker.append_bytes(settings.wav_source_bytes(raw_bytes)):
    pass  # 별도 전송 큐로. settings.server 에 주소가 있다
```

`s24_32le`(ALSA `S24_LE`) 입력은 1초 WAV에 넣을 때 3바이트 24-bit로 다시 묶는다. 값은 바뀌지 않는다. `float32le` 입력은 WAV 묶음을 지원하지 않는다.

## 스피커 소리가 원음과 달라지는 부분

기본값에서 스테레오 출력은 `(원음 × 0.75 + 대역 통과음 × 0.25) × 0.5` 이다.

- **크기**: 전체가 약 -6dB(절반)로 작아진다(`output_gain`).
- **음색**: 대역(120–2800Hz) 밖, 특히 고음(약 3kHz 이상)이 대역 안보다 약 2.5dB 더 작아진다. 48kHz 합성음으로 잰 결과: 500Hz -6.1dB, 1kHz -5.9dB, 5kHz·10kHz -8.5dB. 63탭 필터는 48kHz에서 저음 쪽 경계를 가르지 못해 50Hz도 거의 줄지 않는다(-6.2dB).
- **지연**: 필터 정렬 때문에 `(fir_taps-1)/2` 샘플(48kHz에서 약 0.65ms)이 늦다. 블록 크기만큼의 버퍼링 지연은 따로다.
- **찌그러짐**: 피크 제한은 `output_peak_limit × 0.8` 을 넘는 소리부터 누른다. 기본값(gain 0.5, 한계 0.8)에서는 출력이 0.5를 넘지 않아 작동하지 않는다. gain을 0.64보다 크게 올리면 큰 소리가 눌리며 파형이 바뀐다. 입력이 ±1을 넘는 값은 잘린다.
- **바뀌지 않는 것**: 좌·우는 각각 좌·우 마이크만 쓴다. 섞거나 패닝하지 않는다. 중앙 마이크는 스테레오에 들어가지 않는다.
- 원음 그대로를 원하면 `processed_mix = 0`, `output_gain = 1` 로 둔다. 이때는 0.65ms 지연도 없다.

## 처리 함수

`ProcessorConfig`를 만들어 `MaekAudioProcessor`에 준다. 각 캡처 블록을 **시간 순서대로 같은 인스턴스**의 `process()`에 넣는다. 필터·진동 완화 상태가 블록 사이에 유지된다. 스트림이 끊기면 `reset()`을 호출한다.

```python
import numpy as np
from maek_audio import MaekAudioProcessor, ProcessorConfig

processor = MaekAudioProcessor(ProcessorConfig(
    sample_rate_hz=48000,  # 예시. 기술팀 실제 값으로 교체
    channel_order=("left", "center", "right"),  # 실제 배선 순서로 교체
))

# 아래 배열은 캡처 어댑터가 만들어서 준다고 가정한다.
pcm = np.zeros((480, 3), dtype=np.float32)
result = processor.process(
    pcm,
    sample_rate_hz=48000,
    capture_start_time_ns=None,  # 첫 샘플의 실제 캡처 시각이 있으면 전달
    is_moving=False,
    motor_active=False,
)

stereo = result.stereo_pcm          # (480, 2), float32, 왼쪽·오른쪽
motors = (result.haptic_left, result.haptic_right)  # 각각 0–1
```

`stereo_pcm`은 좌·우 마이크만 재생에 사용하고, 설정된 만큼 대역 처리·원음 혼합·gain·피크 제한을 적용한다. 전기 출력이나 실제 음압을 보장하지 않는다. `haptic_left/right`는 모터 드라이버에 넘길 **목표 강도**다. PWM 값이나 실제 진동 세기가 아니다. 중앙 마이크 성분은 양쪽 목표 강도에 함께 보탠다. 좌우 소리가 함께 들어오면 한 방향만 선택하지 않고 양쪽이 반응한다.

`direction_deg`는 기구 앞을 0°, 오른쪽을 양의 각으로 나타낸 **한 개 우세 음원의 추정 각도**다. `mic_positions_m`을 실제 위치로 넣고 `channels_synchronized=True`로 설정하고, 신호 일관성 검사를 통과할 때만 값이 나온다. 그 외에는 `None`이다. 현재 진동 출력은 불확실한 각도에 의존하지 않고 세 채널의 대역별 레벨을 사용한다. `level_bias`는 좌·우 레벨 차의 표시값(-1…1)이지 각도가 아니다. 반향·여러 음원·장치 이동·작은 마이크 간격에서는 각도 성능을 실측해야 한다.

움직임 또는 모터 작동 상태를 인자로 주면 진동 강도를 낮춘다. 이는 이동 소음 제거가 아니라 **오반응 완화**다. 기존 출력 신호를 참조하는 AEC는 구현하지 않았으므로 `aec_active`는 항상 `False`다. 피크 제한만으로 하울링을 막을 수 없다. 스피커 재생 신호·마이크 위치·오디오 지연을 확보한 뒤 별도 AEC와 피드백 시험이 필요하다.

### 실제 PCM이 16-bit little-endian interleaved일 때의 어댑터 예시

아래는 **입력 형식이 확정됐을 때만** 적용한다. 24-bit가 32-bit 단어 어디에 정렬되는지 등은 아직 모른다.

```python
# raw_bytes: 프레임 순서로 L,C,R가 반복되는 s16le 바이트라고 확인된 경우
raw_i16 = np.frombuffer(raw_bytes, dtype="<i2").reshape(-1, 3)
normalized = raw_i16.astype(np.float32) / 32768.0
result = processor.process(normalized, sample_rate_hz=48000)
```

## 심에 보낼 1초 WAV

`PcmWavChunker`는 **처리 전 PCM**을 WAV에 넣는다. 출력 PCM을 재입력하지 않는다. 이 함수는 파일 저장이나 네트워크 전송을 하지 않으므로 오디오 콜백을 서버 응답으로 막지 않는다. 호출자는 완료된 `WavChunk`를 별도 전송 큐에 즉시 넘겨야 한다.

```python
from maek_audio import PcmWavChunker

chunker = PcmWavChunker(
    sample_rate_hz=48000,
    channel_order=("left", "center", "right"),
    sample_width_bytes=2,  # s16le가 확인된 경우에만
    capture_start_time_ns=None,
)

completed = chunker.append_bytes(raw_bytes)
for chunk in completed:
    # chunk.wav_bytes: 3채널 WAV 파일 자체
    # chunk.sequence, start_sample_index, start_time_ns: 별도 전송 메타데이터
    # 여기에 네트워크 호출을 직접 넣지 말고 비동기 전송 큐에 전달
    pass
```

WAV 안에는 3채널 수·샘플레이트·샘플 폭이 들어가지만 `left/center/right`라는 **채널 이름과 캡처 시각은 들어가지 않는다**. 따라서 `WavChunk`의 `channel_order`, `sequence`, `start_time_ns`를 심 서버에 별도 메타데이터로 함께 보내야 한다. 16/24/32-bit **정수 PCM little-endian**을 받을 수 있다. `append_frames()`는 int16/int32 배열용이고, packed 24-bit는 `append_bytes()`를 쓴다. Float PCM은 포맷이 확정되기 전에는 변환하지 않고 거절한다. 블록마다 첫 샘플 번호를 알면 `first_sample_index`로 주어 누락·중복을 감지한다. 종료할 때만 `flush_partial()`로 1초 미만의 마지막 WAV를 받으며 `complete=False`로 표시된다.

## 기술팀에서 필요한 입력 정보

1. 라즈베리파이가 내보내는 PCM 예시 1~2초와 자료형(예: s16le, packed 24-bit, 24-bit-in-32-bit, float32), 바이트 순서와 유효 비트 정렬.
2. 샘플레이트, 한 호출의 프레임 수, 채널 순서, 채널 간 시간 동기화 방식. 각 블록의 첫 샘플 번호와 캡처 시각을 제공할 수 있는지.
3. 세 마이크 중심의 실제 좌표(기구 앞을 +y, 오른쪽을 +x, 단위 m), 모터 작동 시점, 스피커 재생 신호를 처리 모듈에 되돌려줄 수 있는지.
4. 스피커 출력이 요구하는 PCM 자료형·샘플레이트, 진동 드라이버의 명령 범위·갱신 속도, 심 서버의 업로드 주소·방법·메타데이터·재전송 규칙.

## 설치와 확인

Python 3.9 이상과 NumPy가 필요하다. Raspberry Pi OS에서 준비한 Python 환경에서 이 폴더로 이동해 `python -m pip install .`을 실행한다. 네트워크가 없는 Pi는 NumPy 설치 방식을 기술팀 환경에 맞춰 준비해야 한다.

```text
python -m unittest discover -s tests -v
python examples/synthetic_demo.py
```

현재 테스트는 설정 파일 읽기·형식 변환·모터 값 변환과, 합성 신호의 좌·우·전방·복수 입력, 출력 제한, 동기화가 없는 각도 억제, 원본 1초 WAV 경계와 누락 감지를 본다. 실기에서 추가로 측정할 것은 수집→스피커/모터 지연, Pi CPU 사용량, 지속 운전, 하울링, 이동 소음, 실제 방향 오차다.
