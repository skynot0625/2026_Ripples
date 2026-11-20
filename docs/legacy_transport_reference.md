# Historical 55-slot transport notes

These are original 2026-11-10 notes; current hardware uses 65 slots.
Use README.md and docs/tower.md for the current installation.


LED CENTRAL — 상대방/AI에게 전달하는 단일 Python 파일 (2026-11-10)
================================================================
외부 CSV, 다른 프로젝트 Python 파일, 설정 파일이 필요 없습니다.
2026-11-10 배치의 독립 스냅샷입니다. 배치 변경 시 내장 _SITES/행별 길이와 펌웨어를 함께 갱신하세요.
이 파일은 PC 데이터 생성/변환/USB 송신용입니다. Arduino 펌웨어는 포함하지 않습니다.
보드에 설치된 펌웨어와 version을 맞추세요: rgb565=LED2, pinmask=LED3.

[전력 경고 / 반드시 읽기]
전체 최대 밝기 흰색 점등은 큰 전력을 요구할 수 있습니다. 전원·배선·커넥터·분기 보호
용량 확인 전 전체 최대 밝기로 켜지 마세요. 기본 밝기32도 전류 제한 장치가 아닙니다.
물리 LED8,640개 중 첫 LED180개는OFF, 가시 LED8,460개를 사용합니다.
MUX는 데이터만 전환합니다. 선택되지 않은 스트립도 마지막 색으로 계속 켜집니다.
전력을16으로 나누면 안 됩니다. 통신 단절/포트 닫기는 자동 소등이 아닙니다.

[두 버전의 차이]
1) RGB565 / LED2: PC가RGB565로포장, 보드가RGB복원/첫검정픽셀/밝기32/GRB/파형처리.
   payload21,120B. PC에서밝기32를다시곱하지않습니다. 더어둡게하려면입력RGB세기를낮춥니다.
2) PC 핀 마스크 / LED3: PC가밝기/패딩/GRB/12라인비트정렬까지계산합니다.
   보드는고정펄스확장+DMA출력만합니다. setBrightness와보드강제밝기상한이없습니다.
   PC 기본 brightness32/max_brightness32를 유지하세요. payload32,256B.
   12bit는색상비트수가아니라12개출력핀의상태입니다. 마스크포장은무손실입니다.

[설치]
    python -m pip install "numpy>=2,<3" "pyserial>=3.5,<4"
USB를열지않는변환/자가검사는NumPy만설치해도됩니다. import하면USB를열지않습니다.

[입력 계약 — 데이터 생성자는 이것만 만들면 됩니다]
    frame.shape == (12,16,55,3), dtype=np.uint8, RGB순서, 값0..255
    frame[Zone-1, C포트-1, j, RGB]
    j0 = 물리LED2번(첫가시LED). 숨겨진물리LED1번은입력에넣지않습니다.
    물리N개스트립의유효j는0..N-2. 나머지패딩/미사용포트는자동으로0처리합니다.
    uint16 (12,16,55)는packed RGB565; uint8/bool (12,16,55)는흰색밝기입니다.
    float입력은0..1입니다. NaN/음수/범위초과/잘못된크기는거부합니다.
    원본배열은수정하지않습니다. 출력포트매핑은아래_SITES에모두내장되어있습니다.
    기본 Zone1->DD0 ... Zone12->DD11, C1->MUX0 ... C16->MUX15.

[다른 프로그램에서 사용 — 이 파일을 옆에 두세요]
    import numpy as np
    from ripples.hardware.transport import address, RGB565Encoder, PinMaskEncoder
    frame = np.zeros((12,16,55,3), dtype=np.uint8)
    z,p = address('A6')
    frame[z,p,0] = [255,0,0]  # A6의 첫가시LED하나만빨강
    old_packet = RGB565Encoder().encode_packet(frame, sequence=0)
    new_packet = PinMaskEncoder(brightness=16,max_brightness=32).encode_packet(frame,sequence=0)

[직접 USB 송신 — 해당 펌웨어가 보드에 먼저 설치되어 있어야 합니다]
    from ripples.hardware.transport import CentralUSB, PinMaskUSB
    with CentralUSB('/dev/cu.usbmodem11301') as link:  # LED2
        link.send(frame)
        link.send(np.zeros_like(frame))
    # LED3를쓸때는위클래스대신 PinMaskUSB(...,brightness=16,max_brightness=32).
    # 두클래스모두검정프레임ACK로연결을확인한뒤사용자패턴을보냅니다.

[병렬 생성/송신/ACK/미리보기]
    from ripples.hardware.transport import LiveCentral, PinMaskUSB
    port = '/dev/cu.usbmodem11301'
    with LiveCentral(port, transport_factory=PinMaskUSB, brightness=16) as live:
        live.publish(frame)  # 원본을복사해최신프레임사서함에등록
        item = live.poll_displayed()
        if item:
            ack, actual_rgb = item  # 출력완료ACK와그프레임의실제색근사
            # GUI메인스레드에서표시. 이미밝기적용됨: 32/255를다시곱하지않습니다.
    # 기본 LiveCentral(port)는RGB565. 종료시검정프레임ACK까지확인합니다.
    # 생성/USB송신/ACK수신은분리. GUI그리기는호출자메인스레드에서수행합니다.
    # in-flight최대2개+최신미전송1개. 오래된미전송프레임은교체해지연누적을막습니다.

[바이트 프로토콜 — 다른 언어로 구현할 AI도 이 규칙을 따르면 됩니다]
헤더20B: little-endian <4sIHHII = magic,seq,N,flags,payload_bytes,zlibCRC32(payload).
LED2: magic='LED2', N55, flags6, payload [DD][MUX][j] little-endian uint16 RGB565.
LED3: magic='LED3', N55, flags11. PC에서검정첫픽셀포함물리56개로변환합니다.
      마스크순서 [MUX0..15][물리k0..55][G,R,B][bit7..0]. mask bit0=DD0,bit11=DD11.
      연속12bit마스크A,B를3B로포장:
      byte0=A&255; byte1=(A>>8)|((B&15)<<4); byte2=B>>4.
      예: A0xA35,B0x6C9 -> 35 9A 6C. payload32,256B,헤더포함32,276B.
      실험LEDT/flags10과호환되지않습니다.
RX는수신완료일뿐입니다. 전체16슬롯출력/래치완료는아래OK로확인합니다:
    OK seq rx_us scan_us start_us end_us rx_end_us crc_hex
seq/CRC가맞는ACK만완료처리합니다. 잘못된CRC/부분프레임은보드가표시하지않습니다.
송신측코드는자동재전송하지않으며오류시연결을닫습니다.

[함수/클래스 안내]
address(id) / layout_positions(): 내장배치에서배열주소/좌표/행별길이조회.
as_rgb(): 입력검사와RGB8정규화. rgb565_pack/unpack():5/6/5색상변환.
RGB565Encoder: 마스킹->DD매핑->RGB565포장. 보드가밝기32를적용.
PinMaskEncoder.preview_frame: 길이마스킹과PC밝기적용.
PinMaskEncoder.physical_rgb: 검정첫픽셀삽입, MUX/DD/물리56픽셀배열구성.
PinMaskEncoder.encode_payload: GRB->핀비트마스크->12bit포장.
각encode_packet(): 헤더/seq/CRC추가. encode_payload(): 헤더없는바이트만반환.
두encoder의preview_frame은모두밝기적용RGB8이며보드색의소프트웨어근사입니다.
전원전압/실물LED의감마와색보정까지보장하는광학모델은아닙니다.

[오프라인 확인/변환 — 자동점등데모는없습니다]
    python LED_CENTRAL_HANDOFF.py --self-test
    python LED_CENTRAL_HANDOFF.py --version rgb565 --file frame.npy --output frame.led2
    python LED_CENTRAL_HANDOFF.py --version pinmask --file frame.npy --output frame.led3 --brightness 16
    python LED_CENTRAL_HANDOFF.py --version pinmask --file frame.npy --port /dev/cu.usbmodem11301
마지막명령은프레임을출력후유지합니다. 소등하려면검정프레임을명시적으로보내세요.

단위는cm/mm미확정. 천장면인접간격12, 수직LED간격5, 최하단높이50.
현재길이에맞춘모델기울기28.758918도. 실제설치/배선확인과제안매핑은구분합니다.
