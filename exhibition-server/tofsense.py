"""천장 거리 센서 receiver (2026-11-10): Nooploop TOFSense-F2 P on a serial port → the "distance" input channel.

The sensor sends NLink_TOFSense_Frame0 in UART active-output mode (its default: 921600 baud, 3.3 V TTL):
16 bytes = 0x57 0x00 · reserved · id · system time (4) · distance (3 bytes, little-endian signed, mm) · status ·
signal strength (2, little-endian) · precision · checksum (low byte of the sum of bytes 0–14).
Layout and validity as in ArduPilot's AP_RangeFinder_NoopLoop (status 255 or distance ≤ 0 = no reading).

The channel gets metres (the same value the Mac's mock gives: sensor.up_distance), or None for a frame that says
"no reading" (the layers then show their no-measurement look; no value is made up). No port / no frames: the
channel reports the error and the reader keeps trying (plugging the sensor in later works without a restart).
Standard library only (termios), so the Pi needs no extra package.
"""

from __future__ import annotations

import glob
import os
import select
import termios
import threading
import time

FRAME_LEN = 16
HEADER = (0x57, 0x00)
MAX_M = 60.0                      # longer than any F2 P range: anything above is a bad frame
BAUDS = (921600, 115200, 460800, 230400)   # the factory setting first, then the others NAssistant can set
# "auto": a USB serial adapter first (plugged in on purpose), then the GPIO UART (pins 8 / 10)
AUTO_PORTS = ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA0", "/dev/serial0", "/dev/ttyS0")


def checksum_ok(frame: bytes) -> bool:
    return len(frame) == FRAME_LEN and sum(frame[:15]) & 0xFF == frame[15]


def decode(frame: bytes) -> dict:
    """One checked frame → {"m": metres or None, "status", "strength", "id"}."""
    mm = int.from_bytes(frame[8:11], "little", signed=True)
    status, strength = frame[11], frame[12] | frame[13] << 8
    ok = status != 255 and 0 < mm < MAX_M * 1000
    return {"m": round(mm / 1000, 3) if ok else None, "status": status, "strength": strength, "id": frame[3]}


class FrameParser:
    """Bytes in, decoded frames out; resynchronises on the 0x57 0x00 header after noise or a bad checksum."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad = 0

    def feed(self, data: bytes) -> list[dict]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(bytes(HEADER))
            if i < 0:
                del self.buf[:max(0, len(self.buf) - 1)]   # keep a last 0x57 that may start the next header
                return out
            del self.buf[:i]
            if len(self.buf) < FRAME_LEN:
                return out
            frame = bytes(self.buf[:FRAME_LEN])
            if checksum_ok(frame):
                out.append(decode(frame))
                del self.buf[:FRAME_LEN]
            else:
                self.bad += 1
                del self.buf[:1]


def candidate_ports(spec: str) -> list[str]:
    if spec and spec != "auto":
        return [spec]
    seen = []
    for pattern in AUTO_PORTS:
        for p in sorted(glob.glob(pattern)):
            real = os.path.realpath(p)
            if real not in (os.path.realpath(s) for s in seen):
                seen.append(p)
    return seen


def open_port(path: str, baud: int) -> int:
    """Raw 8N1, no flow control, non-blocking reads."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        speed = getattr(termios, f"B{baud}", None)
        attrs[0] = 0                                              # iflag
        attrs[1] = 0                                              # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL   # cflag: 8 bits, receiver on, no modem lines
        attrs[3] = 0                                              # lflag: raw
        if speed is not None:
            attrs[4] = attrs[5] = speed
        elif not os.isatty(fd):
            raise OSError(f"{baud} baud is not available here")
        # (no constant for that speed but a terminal: a pseudo terminal in the tests, where speed means nothing)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIFLUSH)
    except (termios.error, OSError) as exc:
        os.close(fd)
        raise OSError(str(exc)) from exc
    return fd


def probe(path: str, baud: int, seconds: float = 1.5) -> tuple[int | None, list[dict]]:
    """Open the port at that speed and wait for good frames. → (fd kept open, frames) or (None, [])."""
    try:
        fd = open_port(path, baud)
    except OSError:
        return None, []
    parser, frames, end = FrameParser(), [], time.monotonic() + seconds
    while time.monotonic() < end and len(frames) < 3:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                frames += parser.feed(os.read(fd, 512))
            except OSError:
                break
    if len(frames) >= 3:
        return fd, frames
    os.close(fd)
    return None, []


def find(spec: str = "auto", baud: int | None = None, seconds: float = 1.5) -> tuple[str, int, int, list[dict]] | None:
    """The first port / speed that gives good frames → (port, baud, fd, frames), else None."""
    for path in candidate_ports(spec):
        for b in ([baud] if baud else BAUDS):
            fd, frames = probe(path, b, seconds)
            if fd is not None:
                return path, b, fd, frames
    return None


class TofSenseReader(threading.Thread):
    """Keeps the channel fed from the sensor; finds the port again after it disappears (cable out and back in)."""

    def __init__(self, channel, stop: threading.Event, port: str = "auto", baud: int | None = None, log=print) -> None:
        super().__init__(daemon=True)
        self.channel, self.stop, self.port, self.baud, self.log = channel, stop, port, baud, log
        self.where = None

    def run(self) -> None:
        self.channel.source = "TOFSense-F2 P"
        last_msg = None
        while not self.stop.is_set():
            found = find(self.port, self.baud)
            if found is None:
                ports = candidate_ports(self.port)
                msg = (f"센서 신호 없음 (찾아본 포트: {', '.join(ports)}) · 배선 TX↔RX · 5V · GND 확인" if ports
                       else "센서 포트 없음 · USB 어댑터를 꽂거나 GPIO UART를 켜세요 (install.sh)")
                self.channel.fail(msg)
                if msg != last_msg:
                    self.log(f"천장 거리 센서: {msg}", flush=True)
                    last_msg = msg
                self.stop.wait(2.0)
                continue
            path, baud, fd, frames = found
            self.where = f"{path} · {baud}"
            self.log(f"천장 거리 센서: {path} · {baud} baud 에서 받는 중", flush=True)
            last_msg = None
            for f in frames:
                self.channel.update(f["m"])
            self._read(fd)
            os.close(fd)
            self.where = None
            self.log("천장 거리 센서: 신호 끊김 · 다시 찾는 중", flush=True)

    def _read(self, fd: int) -> None:
        parser, quiet_since = FrameParser(), time.monotonic()
        while not self.stop.is_set():
            r, _, _ = select.select([fd], [], [], 0.25)
            if r:
                try:
                    data = os.read(fd, 1024)
                except OSError:
                    return   # unplugged
                if not data:
                    return
                frames = parser.feed(data)
                for f in frames:
                    self.channel.update(f["m"])
                if frames:
                    quiet_since = time.monotonic()
            if time.monotonic() - quiet_since > 2.0:
                self.channel.fail("센서 신호 끊김 (2초 넘게 값 없음)")
                return


def start(channel, stop: threading.Event, port: str = "auto", baud: int | None = None) -> TofSenseReader:
    reader = TofSenseReader(channel, stop, port, baud)
    reader.start()
    return reader


def frame(mm: int, status: int = 0, strength: int = 1200, sensor_id: int = 0, t_ms: int = 0) -> bytes:
    """A frame as the sensor sends it (tests, the check script's self-test)."""
    body = bytes([0x57, 0x00, 0xFF, sensor_id]) + (t_ms & 0xFFFFFFFF).to_bytes(4, "little") \
        + mm.to_bytes(3, "little", signed=True) + bytes([status]) + strength.to_bytes(2, "little") + bytes([0])
    return body + bytes([sum(body) & 0xFF])
