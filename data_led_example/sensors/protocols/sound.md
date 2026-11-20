# 서버 수신 WAV 파일 형식 요약

현재 제공한 `mic3.wav_upload` 모듈 기준입니다. 전송 대상은 스피커용 가공 음성이 아닌 **마이크 3채널 원본 PCM**입니다.

| 항목 | 형식 |
|---|---|
| 파일 형식 | WAV, 비압축 PCM |
| 샘플레이트 | 44,100Hz |
| 샘플 형식 | Signed 16-bit, Little-endian |
| 채널 수 | 3채널 |
| 채널 순서 | MIC1 → MIC2 → MIC3 |
| 물리 입력 | Teensy 핀 8 → 핀 6 → 핀 9 |
| 기본 파일 길이 | 1초 |
| 기본 프레임 수 | 44,100프레임 |
| 기본 파일 크기 | 264,644바이트 |

한 프레임은 같은 시점의 세 마이크 샘플로 구성되며 총 6바이트입니다.

```text
[MIC1: 2바이트][MIC2: 2바이트][MIC3: 2바이트] 반복
```

세 채널은 섞이지 않고 독립적으로 저장됩니다. MIC1·2·3이 실제 좌·중앙·우 중 어디에 해당하는지는 설치 배치를 별도로 확인해야 합니다.

## 전송 및 저장

- HTTP POST의 body 전체가 WAV 파일 바이너리입니다. multipart나 Base64가 아닙니다.
- `Content-Type: audio/wav`
- `Idempotency-Key`: 파일 고유 ID인 `chunk_id`
- `X-Audio-Metadata`: 파일 정보가 담긴 JSON 헤더

제공한 테스트 수신기는 다음 구조로 저장합니다. 운영 서버의 저장 경로는 서버 구현에 따라 달라집니다.

```text
저장폴더/<chunk_id>/
├── audio.wav
└── metadata.json
```

## 주요 메타데이터

| 필드 | 의미 |
|---|---|
| `device_id` | 수집 장치 식별자 |
| `session_id` | 연속된 수집 구간 식별자 |
| `chunk_id` | 파일 고유 ID. 재전송에도 유지 |
| `segment_index` | 세션 내 파일 순번, 0부터 시작 |
| `start_frame` | 세션 내 파일 시작 프레임 위치 |
| `frames` | 해당 파일의 실제 프레임 수 |
| `received_at_utc` | Pi가 해당 파일의 첫 데이터를 처리한 UTC 시각 |
| `sha256` | WAV 파일 전체 바이트의 SHA-256 |
| `demo` | `true`이면 실제 마이크가 아닌 합성 시험 신호 |

## 읽을 때 주의할 사항

- 종료나 USB 단절 시 **1초보다 짧은 파일**이 생길 수 있습니다. 길이는 WAV 헤더 또는 `frames / 44100`으로 계산합니다.
- 파일명이나 서버 도착순 대신 **장치·세션별 `segment_index` 또는 `start_frame`**으로 정렬합니다.
- 재연결·입력 누락·프로그램 재시작으로 `session_id`가 바뀔 수 있습니다. 서로 다른 세션을 끊김 없는 녹음으로 간주하면 안 됩니다.
- `received_at_utc`는 정확한 마이크 샘플링 시각이 아닙니다.
- 재전송으로 같은 `chunk_id`가 다시 올 수 있습니다. 동일 ID·동일 해시는 중복 저장하지 않습니다.

## Python 읽기 예시

```python
import wave
import numpy as np

with wave.open("audio.wav", "rb") as wav:
    assert wav.getnchannels() == 3
    assert wav.getsampwidth() == 2
    assert wav.getframerate() == 44100
    assert wav.getcomptype() == "NONE"

    raw = wav.readframes(wav.getnframes())

pcm = np.frombuffer(raw, dtype="<i2").reshape(-1, 3)

mic1 = pcm[:, 0]
mic2 = pcm[:, 1]
mic3 = pcm[:, 2]

# 필요 시 약 -1~1 범위의 실수로 변환
normalized = pcm.astype(np.float32) / 32768.0
```

## 서버 저장 완료 응답

서버가 WAV와 메타데이터를 저장한 후 HTTP `200` 또는 `201`과 아래 JSON을 반환해야 합니다. Pi는 값이 일치하는 응답을 받아야 로컬 대기열에서 파일을 삭제합니다.

```json
{
  "stored": true,
  "chunk_id": "요청과 동일한 파일 ID",
  "sha256": "저장한 WAV의 SHA-256"
}
```
