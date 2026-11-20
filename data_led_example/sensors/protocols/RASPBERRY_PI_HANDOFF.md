# BLE 비콘 수신 핵심 사양

2026-11-18 · ESP32-C3 · 노드 1~5 · +12dBm · 기준 간격 100ms

## 어디로 브로드캐스팅하는가

**2.4GHz BLE 기본 광고 채널 37·38·39 전체로 같은 패킷을 송신한다.** 특정 Raspberry Pi나 MAC 주소를 목적지로 지정하지 않는다. 근처의 BLE 스캐너가 광고를 수신한다. Wi-Fi 브로드캐스트나 IP/포트 통신이 아니다.

| BLE 채널 | 중심 주파수 |
| --- | --- |
| 37 | 2402MHz |
| 38 | 2426MHz |
| 39 | 2480MHz |

소스의 `channel_map=0x07`은 세 채널을 모두 사용한다는 뜻이다. 비트 `0x01/0x02/0x04`가 각각 37/38/39에 대응한다. **노드마다 채널을 나누지 않는다.** 하나의 광고 이벤트에서 같은 데이터가 채널별로 전송되므로 콜백 횟수는 송신 이벤트 수와 같지 않다. BLE의 연결용 데이터 채널 0~36은 이 비콘에서 사용하지 않는다. [Bluetooth SIG 광고 설명](https://www.bluetooth.com/blog/advertising-works-part-1/)

## 내부 설정과 현재 값

| 설정 | 현재 펌웨어 | 의미 |
| --- | --- | --- |
| 무선 방식 / PHY | BLE Legacy Advertising / LE 1M | 1Mbps PHY. Wi-Fi·Bluetooth Classic 사용 안 함 |
| 역할 / PDU | Broadcaster / `ADV_NONCONN_IND` | 광고만 송신, 연결 및 스캔 응답 없음 |
| 광고 채널 | 37·38·39, `channel_map=0x07` | 세 기본 광고 채널 사용 |
| 광고 간격 | `itvl_min=itvl_max=160 + prime(group_id)` | 단위는 0.625ms. 실제 간격에는 무작위 지연 추가 |
| 송신 출력 | **+12dBm**, 약 15.85mW | 안테나에 공급하는 공칭 출력 설정. 수신 RSSI와 다름 |
| 장치 이름 / Scan Response | 없음 / 없음 | 이름 조회·GATT 연결로 찾지 않음 |
| 데이터 형식 | 31바이트 AdvData, `0x21` Service Data | 아래 UUID 및 10바이트 사용자 페이로드 |
| 주소 / 개별 ID | 공장 Bluetooth MAC 기반 | 하드웨어 진단용. 논리 노드 식별에는 사용하지 않음 |
| 절전 | Modem sleep + 자동 light sleep 활성화 | 광고는 컨트롤러가 예약. USB 호스트 연결 중에는 light sleep 제한 |

현재 코드에서 바꿀 수 있는 주요 옵션은 그룹 번호, 기준 간격, 출력(`-6/0/+3/+6/+9/+12dBm`), 이름·스캔 응답 활성화, 기준 RSSI, light sleep이다. 채널 마스크는 `main/beacon.c`에서 설정한다. 이름 응답을 켜면 광고 모드도 scannable로 바뀌므로 현재 사양과 달라진다. PHY 변경은 현재 Legacy 광고 설정의 단순 출력 옵션이 아니다. 연결 간격·MTU·페어링 같은 설정은 현재의 비연결 광고 수신에는 필요하지 않다.

수신기 쪽의 **스캔 간격·스캔 창·active/passive·중복 보고**는 별도 설정이다. passive는 수신만 하고, active는 응답 가능한 광고에 추가 정보를 요청할 수 있다. 우리 비콘은 요청에 응답하지 않으며, 수신에 연결이나 페어링이 필요 없다.

### 노드별 광고 간격

`기준 100ms + n번째 소수 × 0.625ms`, n은 group_id다.

| group_id | 소수 | 설정 단위 | 광고 간격 |
| --- | ---: | ---: | ---: |
| 1 | 2 | 162 | 101.250ms |
| 2 | 3 | 163 | 101.875ms |
| 3 | 5 | 165 | 103.125ms |
| 4 | 7 | 167 | 104.375ms |
| 5 | 11 | 171 | 106.875ms |

실제 이벤트에는 BLE 표준의 **0~10ms 무작위 지연**이 추가된다. 정확한 10Hz가 아니며 소수 오프셋도 충돌 방지를 보장하지 않는다. [Bluetooth SIG, 광고 타이밍 7.6.2.2.2절](https://www.bluetooth.com/wp-content/uploads/2023/03/bluetooth-le-regulatory-aspects-document.pdf)

## 패킷 디코딩과 논리 노드 식별

공통 Service UUID: **`bf46fcd8-bd8d-5695-a08b-a7c1d419be4e`**

**Service Data UUID 확인 → 길이·버전 검증 → 패킷의 group_id로 노드 구분 → 수신 API의 RSSI 수집** 순서로 처리한다. MAC이나 기존 개별 ID 허용 목록으로 필터링하지 않는다.

Python Bleak의 `adv.service_data[UUID]`는 UUID를 제외한 **10바이트**다.

| 인덱스 | 자료형 | 의미 |
| --- | --- | --- |
| 0 | uint8 | 프로토콜 버전 `1` |
| 1~2 | uint16 little-endian | **group_id: 논리 노드 번호** |
| 3~8 | 6바이트 | beacon_id: 물리 장치 진단용, 수신 순서대로 HEX 표기 |
| 9 | int8 | 기준 RSSI. **127은 미보정**, 송신 출력이나 실제 수신 RSSI가 아님 |

```python
import struct

UUID = "bf46fcd8-bd8d-5695-a08b-a7c1d419be4e"

def decode(adv):
    payload = adv.service_data.get(UUID)
    if payload is None or len(payload) != 10:
        return None
    version, group, identity, reference = struct.unpack("<BH6sb", payload)
    if version != 1 or group == 0 or adv.rssi is None or adv.rssi == 127:
        return None
    return {"node": group, "rssi_dbm": adv.rssi,
            "hardware_id": identity.hex().upper(),
            "reference_rssi_dbm": None if reference == 127 else reference}
```

예: `01 03 00 AA BB CC DD EE FF 7F` → **노드 3**, 물리 ID `AABBCCDDEEFF`, 기준 RSSI 미보정. 실제 RSSI는 `adv.rssi`에서 얻는다.

원시 AdvData를 직접 해석한다면 AD 필드를 순회해 타입 `0x21`을 찾는다. 그 안의 첫 16바이트는 UUID 전송 순서 `4E BE 19 D4 C1 A7 8B A0 95 56 8D BD D8 FC 46 BF`이고, 나머지가 위 10바이트다. 이 구조는 iBeacon Manufacturer Data 형식이 아니다.

## Raspberry Pi 수신 시 지킬 것

- BlueZ + Bleak 스캐너를 계속 유지하며 `adv.service_data`에서 UUID를 검사한다. 이름 및 별도 `service_uuids` 목록만으로 찾지 않는다. 우리 UUID는 Service Data 안에 있다. [Bleak Scanner API](https://bleak.readthedocs.io/en/stable/api/scanner.html)
- 반복 관측을 위해 BlueZ의 `DuplicateData=True`를 사용한다. 이는 중복 검출 해제이며 모든 무선 패킷의 보고를 보장하지 않는다. [BlueZ Adapter API](https://bluez.readthedocs.io/en/latest/adapter-api/)
- **좌표·위치 모델의 키는 `(UUID, group_id)`**다. 모듈 교체 시 같은 위치에 동일 group_id를 설정하면 수신기 MAC 설정 변경 없이 같은 노드로 처리한다. 현재 프로젝트에서 그룹은 구역 묶음이 아닌 개별 노드 번호다.
- 동일 그룹의 서로 다른 hardware_id가 최근 3초 안에 관측되면 `CONFLICT`로 표시하고 위치 계산에서 제외한다. 두 장치의 RSSI를 섞지 않는다. 교체 시 이전 모듈을 먼저 끄며, 새 장치의 필터 버퍼는 새로 시작한다.
- 원시 로그는 수신 시각·수신기 ID·group_id·hardware_id·RSSI를 저장한다. 1초마다 노드별 LAST, 최근 1~2초 중앙값, age(ms)를 집계하고, 3초 이상 미수신이면 `STALE`로 제외한다. 미수신을 0dBm으로 채우지 않는다.
- 고정 비콘의 좌표와 이동 수신기의 RSSI 벡터로 우선 가까운 구역을 판별하고, 현장 지문을 수집해 위치 추정을 개선한다. RSSI를 바로 정확한 거리로 간주하지 않는다. 출력·안테나·모듈·수신기가 바뀌면 재보정한다.
- 패킷에는 좌표·송신 시각·시퀀스·실제 송신 출력이 없다. 설치표와 배포 기록에 따로 관리한다. 콜백 수로 정확한 패킷 손실률을 계산할 수 없다.
