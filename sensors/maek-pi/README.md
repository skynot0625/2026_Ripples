# Teensy 마이크 → Maek 처리 → MAX98357A 스피커

현재 구성에 맞춘 독립 실행 패키지입니다. **초기 설치·배선·재부팅 후 `python3 run.py` 하나로 실행합니다.**

- Raspberry Pi 3B / Debian 13 Trixie / aarch64
- Teensy 4.0: 이전 `mic3_teensy.ino` 펌웨어 그대로 사용
- 마이크 3채널: USB Serial, 44,100Hz, signed 16-bit little-endian
- 출력: Adafruit MAX98357A 3006-P 두 개 + 스피커 두 개
- GUI/Arduino IDE 불필요. 파이의 I2S 오디오 출력은 ALSA/aplay가 담당

첨부한 `maek-audio-pi_0.2.0.zip`은 **PCM 처리 라이브러리**이며 캡처/재생 기능은 없었습니다. 원본 `maek_audio` 코드는 보존하고 Teensy 수신·ALSA 재생·실행 진입점을 추가했습니다. 이 패키지는 기존 `mic3-pi` 폴더와 별도로 설치할 수 있으며 그 폴더에 의존하지 않습니다.

## 1. 설치와 한 파일 실행

ZIP을 Pi 홈 폴더에 복사한 뒤:

```bash
cd ~
unzip maek-teensy-speaker.zip
cd ~/maek-teensy-speaker
bash setup_pi.sh
```

최초 설치는 관리자 권한과 인터넷이 필요합니다. `setup_pi.sh`는 Debian 패키지 `python3-numpy`, `python3-serial`, `alsa-utils`를 설치하고, Pi 부팅 설정을 백업한 뒤 MAX98357A 오버레이를 켭니다. 자동 재부팅은 하지 않습니다.

**아래 배선을 전원을 끈 상태에서 완료하고**, 부팅 설정 변경 후 재부팅하세요.

```bash
sudo reboot
```

다시 접속한 뒤 최초 확인:

```bash
cd ~/maek-teensy-speaker
python3 run.py --list-devices
python3 run.py --check
python3 run.py --test-output
```

테스트음은 왼쪽 1초 / 오른쪽 1초를 두 번 반복합니다. 세 명령은 각각 장치 목록, 설정 확인, 실제 출력 확인용입니다. 평소 실행은 다음 하나입니다.

```bash
python3 ~/maek-teensy-speaker/run.py
```

종료는 Ctrl+C입니다. 다른 폴더에서 실행해도 설정은 `run.py` 옆의 `config`에서 읽습니다. 가상환경이 필요 없습니다. 기존 mic3-pi의 가상환경을 활성화했다면 `deactivate` 후 실행하거나 `/usr/bin/python3 ~/maek-teensy-speaker/run.py`를 사용하세요.

**기존 monitor/record/wav_upload 프로세스는 먼저 종료하세요.** USB Serial 포트 하나를 여러 프로그램이 동시에 읽을 수 없습니다. 이 패키지에는 서버 업로드를 동시에 실행하는 기능을 넣지 않았습니다.

## 2. 전체 배선과 채널

```text
마이크 3개 → Teensy → USB → Raspberry Pi
                                │
                         I2S BCLK/LRCLK/DATA
                                ├── MAX98357A L → 왼쪽 스피커
                                └── MAX98357A R → 오른쪽 스피커
```

마이크 I2S는 Teensy가, 출력 I2S는 Pi가 생성합니다. **두 장치의 I2S 클록을 서로 묶지 않습니다.**

Teensy 입력 배선은 이전과 같습니다.

| 마이크 | SD → Teensy | 기본 처리상 위치 |
|---|---|---|
| MIC1 | 핀 8 | 왼쪽 |
| MIC2 | 핀 6 | 중앙 |
| MIC3 | 핀 9 | 오른쪽 |

SCK는 Teensy 21, WS는 Teensy 20, 마이크 전원은 Teensy 3.3V, GND와 L/R은 GND입니다. **아래 앰프의 5V 전원과 마이크의 3.3V 전원을 혼동하지 마세요.**

### MAX98357A 두 개의 공통 신호선

아래 GPIO 번호는 **Pi BCM 번호**, 물리 핀 번호는 **Pi 40핀 헤더 위치**입니다.

| Pi 신호 | 물리 핀 | 왼쪽 MAX98357A | 오른쪽 MAX98357A |
|---|---:|---|---|
| GPIO18 / PCM_CLK | 12 | BCLK | BCLK |
| GPIO19 / PCM_FS | 35 | LRC / LRCLK | LRC / LRCLK |
| GPIO21 / PCM_DOUT | 40 | DIN | DIN |
| GND | 6 등 | GND | GND |

DIN 하나에 L/R 데이터가 번갈아 나오므로 **앰프 두 개의 DIN을 같은 Pi GPIO21에 연결**합니다. MCLK는 필요 없습니다. 제조사 배선: [Adafruit Pi wiring](https://learn.adafruit.com/adafruit-max98357-i2s-class-d-mono-amp/raspberry-pi-wiring).

### 전원과 스피커

각 앰프 VIN은 안정적인 **5V**로 공급하고 GND를 Pi와 공통 연결합니다. Pi 5V 헤더(물리 2 또는 4)를 사용할 경우 Pi·Teensy·앰프 두 개의 합산 소비 전류를 전원이 감당해야 합니다. 두 앰프를 높은 음량으로 구동하려면 전원 용량을 확인해야 합니다. 별도 5V 앰프 전원을 쓰는 경우에는 **GND를 공통으로 하되 별도 전원의 +5V를 Pi의 +5V와 임의로 병렬 연결하지 마세요.** Teensy 3.3V로 앰프를 공급하지 않습니다.

각 스피커의 두 선을 해당 앰프의 `+`, `−` 출력에 연결합니다. **스피커 −는 GND가 아닙니다. 스피커 −를 Pi GND에 연결하거나 두 앰프의 출력을 서로 연결하면 안 됩니다.**

사용자가 적은 Adafruit 3968은 현재 공식 페이지에는 4Ω / 5W로 표시되어 있어, 보유품에 적힌 3W 표기와 다릅니다. 보유 스피커의 실제 라벨을 우선 확인하세요. 이 프로그램은 낮은 소프트웨어 게인으로 시작하며 출력 전력을 보장하거나 측정하지 않습니다. [제품 3968](https://www.adafruit.com/product/3968)

### 왼쪽/오른쪽 선택: 앰프의 SD/MODE

**마이크 SD(음성 데이터)와 앰프 SD(정지·채널 선택)는 서로 다른 신호입니다.** 앰프 SD를 Pi GPIO21에 연결하지 마세요.

Adafruit 3006-P 모노 앰프는 SD를 비워두면 기본적으로 L/R 평균을 내보냅니다. 두 개 모두 비워두면 스테레오가 되지 않습니다. 본 구성에서는 SD를 저항/전원으로 고정하고 드라이버의 SD 제어를 끕니다.

- 왼쪽 앰프 SD: Pi **3.3V 전원 핀(물리 1 또는 17)**에 연결합니다.
- 오른쪽 앰프 SD: 아래 저항 분압점에 연결합니다. 앰프 보드의 기존 부품은 제거하지 않습니다.

```text
Pi 3.3V ── 22kΩ ──┬── 오른쪽 앰프 SD/MODE
                   │
                  10kΩ
                   │
Pi GND ────────────┘
```

위 22kΩ/10kΩ 구성은 내부 풀다운과 보드의 풀업을 고려해 약 1V가 되도록 잡은 설계값입니다. 전원을 넣고 멀티미터로 오른쪽 SD–GND가 **0.77~1.4V**인지 확인하세요. 왼쪽은 **1.4V 초과**여야 합니다. 오른쪽 분압점은 GPIO 신호핀에 연결하지 않습니다. 저항이 아직 없으면 둘 다 평균 출력으로 시험은 가능하지만 좌우 분리 테스트는 통과하지 않습니다.

GAIN은 우선 두 앰프 모두 연결하지 않고 동일하게 둡니다(보드 기본 게인). 소프트웨어 음량은 기본 0.1입니다. [Adafruit 채널 선택·GAIN 설명](https://learn.adafruit.com/adafruit-max98357-i2s-class-d-mono-amp/pinouts)

## 3. 부팅 설정과 드라이버

자동 설정은 `/boot/firmware/config.txt`를 우선 사용하며 없으면 `/boot/config.txt`를 찾습니다. `max98357a.dtbo` 존재를 검사합니다. 원래 설정은 `config.txt.maek-backup-날짜`로 보관합니다.

원하는 최종 설정은 활성 `[all]` 구역의 다음 두 줄입니다.

```ini
[all]
dtparam=i2s=on
dtoverlay=max98357a,no-sdmode
```

`no-sdmode`는 GPIO4로 SD를 조작하지 않게 하는 옵션입니다. 위와 같이 SD를 고정 선택하는 배선에 맞춘 설정입니다. 앰프가 두 개여도 스테레오 버스를 공유하므로 overlay를 두 번 추가하지 않습니다. [Raspberry Pi 공식 overlay](https://github.com/raspberrypi/linux/blob/rpi-6.12.y/arch/arm/boot/dts/overlays/max98357a-overlay.dts)

기존 `dtparam=audio=on`은 자동 설치가 주석 처리합니다. 기존 `dtoverlay=max98357a`도 중복 활성화하지 않도록 주석 처리합니다. HDMI 설정은 건드리지 않으며 프로그램이 MAX98357A 장치를 골라 사용합니다. `/etc/asound.conf`를 덮어쓰지 않습니다.

설정에 `include`가 있거나 다른 I2S 오디오 overlay가 감지되면 자동 변경을 중단합니다. 이 경우 포함된 파일과 기존 오디오 구성을 확인하여 위 두 줄을 **실제로 활성인 구역**에 수동 반영하세요. Debian 13이라는 정보만으로 Raspberry Pi용 커널/펌웨어가 설치되었다고 보장되지는 않습니다. overlay 파일이 없다면 Pi 커널/펌웨어 구성을 확인해야 하며 프로그램 설치만으로 해결되지 않습니다.

재부팅 후 `aplay -l`에서 `MAX98357A`를 확인하세요. 이름이 다른 경우:

```bash
python3 run.py --list-devices
python3 run.py --device 'plughw:CARD=실제카드ID,DEV=0'
```

이후 `config/device.json`의 `alsa_device`를 해당 값으로 바꾸면 `python3 run.py`만으로 실행할 수 있습니다. `auto`는 MAX98357A만 선택하며 HDMI 등으로 몰래 우회하지 않습니다.

## 4. 출력 동작과 설정

`config/audio.ini`가 **실제 실행 설정**입니다. `config/maek_audio.ini`는 원본 보존/원본 테스트용 빈 템플릿이며 실행에 사용하지 않습니다.

| 설정 | 기본값 | 의미 |
|---|---|---|
| input.sample_rate_hz | 44100 | 현재 Teensy 펌웨어와 일치 |
| input.sample_format | s16le | 현재 USB PCM 형식 |
| input.channel_order | left, center, right | 핀 8,6,9에 연결된 마이크의 실제 위치 |
| tuning.output_gain | 0.1 | 조용한 초기 출력 |
| tuning.processed_mix | 0.25 | 원음 75% + 필터음 25% |
| tuning.output_peak_limit | 0.5 | 디지털 피크 상한 |

**중앙 마이크는 원본 라이브러리 설계대로 스피커에 섞지 않습니다.** MIC1을 왼쪽, MIC3를 오른쪽에 재생합니다. 위치가 다르면 channel_order를 바꾸세요. 예를 들어 핀 8이 오른쪽, 핀 6이 중앙, 핀 9가 왼쪽이면 `right, center, left`로 설정합니다.

`config/device.json`의 `mic_gains`는 **물리 입력 핀 8,6,9 순서**입니다. 0이면 해당 마이크를 음소거하고 1이면 그대로 처리합니다. 임시로 MIC1을 끄려면:

```bash
python3 run.py --mute-mic 1
```

현재 MS3625에서 큰 잡음이 발생했던 문제를 이 프로그램이 해결하는 것은 아닙니다. 문제 채널을 끄고 나머지 출력 경로를 확인할 수 있습니다. 잡음 원인을 해결하기 전에는 출력 게인을 높이지 마세요.

마이크와 스피커가 서로 소리를 주고받으면 하울링이 생길 수 있습니다. **AEC/피드백 제거는 구현되어 있지 않습니다.** 처음에는 스피커를 마이크에서 떨어뜨리고 낮은 음량으로 시험하세요. 디지털 피크 제한만으로 하울링을 방지하지 못합니다.

방향 각도 계산은 기본 비활성입니다. 모터 강도는 원본 라이브러리가 계산하지만 실제 GPIO/PWM/모터를 구동하지 않습니다. 서버 설정 항목도 기록용이며 이 실행기는 업로드하지 않습니다.

## 5. 끊김·지연 처리

- USB 수집 스레드와 스피커 출력 루프를 분리합니다.
- USB 패킷의 CRC와 형식 검사를 그대로 사용합니다.
- 입력 큐는 기본 24패킷(약 70ms)으로 제한하고, 꽉 차면 가장 오래된 입력을 버립니다.
- 큐에서 150ms 이상 지난 입력은 버립니다.
- USB가 없으면 무음을 출력하고 약 2초 간격으로 재연결합니다. 유효 패킷이 5초 동안 없으면 포트를 다시 엽니다.
- ALSA 출력이 종료되거나 2초 이상 정지하면 오류 종료합니다.

**지지직 방지 (`maek_runtime/smooth.py`)**

- 패킷이 빠지거나(번호 건너뜀) 입력이 늦어 무음을 넣어야 할 때, 바로 앞 블록(128프레임, 약 3ms)을 서서히 줄이고 다음 블록을 서서히 키웁니다. 필터 초기화도 그 무음 구간 안에서 일어나므로 파형이 튀지 않습니다. 이를 위해 출력을 한 블록(약 3ms) 늦게 내보냅니다.
- Teensy와 Pi의 클록 차이는 아주 작은 리샘플링(최대 ±0.5%)으로 맞춥니다. 입력 큐가 목표(`device.json`의 `target_queue_packets`, 기본 6패킷 ≈ 17ms)보다 쌓이면 조금 빨리, 모자라면 조금 느리게 재생합니다. 그래서 장시간 실행해도 패킷을 버리거나 언더런이 생기지 않습니다.
- aplay로 가는 파이프를 4KB로 줄여 쓰기가 사운드카드 속도에 맞춰 멈추도록 했습니다(기본 64KB 파이프는 약 0.37초 지연을 숨깁니다).
- 2초마다 찍는 상태 줄: `in_rate`(실측 Teensy 샘플레이트), `ratio`(재생 보정 비율), `queue`, `gaps`(번호 건너뜀 횟수), `underrun`(입력이 늦어 무음을 넣은 횟수), `queue_drop`, `stale`, `crc_bad`, `reconnect`. 지지직이 남으면 이 줄을 보내 주세요.


**원격 소리 설정 (`maek_runtime/control.py`)**

`run.py`가 돌고 있으면 같은 Wi‑Fi의 노트북 브라우저에서 `http://mic.local:8090` 을 연다. 스피커 음량 · 대역 강조 비율 · 대역 범위 · 최대 크기 · 마이크별 음량(끄기)을 바꾸면 재시작 없이 바로 반영되고(짧은 페이드), `config/live.json` 에 저장돼 다시 켜도 유지된다. 「처음 값으로」는 `audio.ini` · `device.json` 값으로 돌린다. 포트는 `device.json` 의 `control_port`(0 = 끔) 또는 `--control-port`.

## 6. 하드웨어 없이 시험

```bash
python3 run.py --demo --seconds 2 --output-wav demo-stereo.wav
```

스피커 대신 파일에 저장합니다. 출력 파일은 기존 파일을 덮어쓰지 않습니다. 데모 입력은 MIC1=440Hz, MIC2=660Hz, MIC3=880Hz이므로 출력에는 왼쪽 440Hz, 오른쪽 880Hz가 나옵니다.

출력 경로만 시험하려면:

```bash
python3 run.py --test-output
```

합성 마이크 신호를 실제 스피커로 재생하려면:

```bash
python3 run.py --demo --seconds 5
```

`--output-wav`는 수신한 데이터만 저장하며 입력 단절 시간을 무음으로 채우지 않습니다. `--seconds`는 실행 시간 기준이라 파일 샘플 길이가 정확히 그 초수와 일치하지 않을 수 있습니다. 실제 원본 3채널 녹음이 목적이면 이전 mic3-pi record 기능을 사용하세요.

## 7. 문제 해결

- **ModuleNotFoundError**: `bash setup_pi.sh` 실행 후 `/usr/bin/python3 run.py`. 별도 가상환경은 시스템 apt 패키지를 못 볼 수 있습니다.
- **MAX98357A가 안 보임**: overlay, 활성 설정 구역, 재부팅, `aplay -l`, Pi 커널 구성을 확인합니다.
- **Permission denied (USB)**: `sudo usermod -aG dialout "$USER"` 후 로그아웃/재로그인. Teensy USB 권한 규칙이 이미 설치된 경우 보통 필요하지 않습니다.
- **ALSA permission denied**: `sudo usermod -aG audio "$USER"` 후 로그아웃/재로그인.
- **Device busy**: 기존 monitor/record/wav_upload 또는 다른 오디오 재생 프로그램을 종료합니다.
- **둘 다 같은 소리**: 두 앰프의 SD를 모두 비워둔 것은 아닌지 확인합니다. --test-output에서 좌우가 번갈아야 합니다.
- **중앙 마이크만 반응하고 스피커가 조용함**: 중앙은 출력에 섞이지 않는 원본 설계입니다.
- **한쪽 큰 잡음**: MS3625/혼용 마이크 문제는 별도 점검 대상입니다. --mute-mic으로 해당 입력을 끄고 확인할 수 있습니다.
- **재생할 때 Pi 재부팅/USB 끊김**: 전원 용량, 5V 전압 강하, 앰프 전원 배선과 접지를 확인합니다.
- **딱딱거리는 소리/underrun**: 시스템 부하, 배선, 전원, 서로 다른 입출력 클록을 확인합니다. 높은 실제 지연과 처리 부하는 Pi에서 측정해야 합니다.

## 8. 패키지 구성·검증

```text
run.py                   # 매번 실행하는 단일 진입점
setup_pi.sh              # 최초 설치
config/audio.ini         # 실제 오디오 설정
config/device.json       # USB/ALSA/입력 게인 설정
maek_audio/              # 사용자가 제공한 0.2.0 처리 라이브러리 그대로
maek_runtime/            # 추가한 USB 캡처·처리 연결·ALSA 출력
tools/enable_i2s.py      # 부팅 설정 백업/수정
tests/                  # 원본 및 추가 테스트
docs/ORIGINAL_README.md  # 원본 설명 보존
```

```bash
python3 -m unittest discover -s tests -v
```

개발 환경(Python 3.9 / NumPy 1.26.4)에서 원본 및 추가 테스트 **30개가 통과**했습니다. ALSA 실행 어댑터는 가짜 aplay 프로세스로 데이터 전달을 검사했으며 실제 오디오 하드웨어 검증은 아닙니다. 합성 입력, PCM 채널 매핑/중앙 제외/음소거, USB CRC·번호 순환·불연속, 큐 제한, 장치 선택, 부팅 설정 변환, 한 파일 진입점 및 WAV 출력을 검사합니다. **실제 Raspberry Pi·Teensy·MAX98357A·스피커에서의 재생, 전원, 지연 및 음질은 이 환경에서 검증하지 못했습니다.** 기존 펌웨어가 동작한다는 전제의 별도 실행 패키지이며 펌웨어 업로드 도구는 포함하지 않습니다.
