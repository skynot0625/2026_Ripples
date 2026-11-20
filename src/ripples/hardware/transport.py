"""USB transport and historical 8,460-neuron address geometry.

LEDUSB(version="rgb565") selects the CURRENT 65-slot encoder.
RGB565Encoder/PinMaskEncoder below are historical 55-slot helpers.
See docs/tower.md before controlling physical LEDs.
"""

from __future__ import annotations
import argparse
import math
import struct
import sys
import threading
import time
import warnings
import zlib
from pathlib import Path
import numpy as np

SHAPE=(12,16,55)
HEADER=struct.Struct('<4sIHHII')
PHYSICAL_SHAPE=(16,12,56,3)
DEFAULT_MAX_BRIGHTNESS=32
PAYLOAD_BYTES=32256
LAYOUT_DATE='2026-11-10'
ROW_PHYSICAL_COUNTS={'A': 56, 'B': 55, 'C': 54, 'D': 53, 'E': 52, 'F': 51, 'G': 50, 'H': 49, 'I': None, 'J': 47, 'K': 46, 'L': 45, 'M': 44, 'N': 43, 'O': 42, 'P': 41, 'Q': 40}

# Each installed strip: (fixed position ID, Zone1..12, C-port1..16, axial q, r).
# Only these 180 positions are installed; central rings 0..2 and exact cross axes are absent.
_SITES=(
    ('B8',1,1,6,-7),
    ('A8',1,2,7,-8),
    ('B9',1,3,7,-7),
    ('A9',1,4,8,-8),
    ('C9',1,5,6,-6),
    ('C10',1,6,7,-6),
    ('B10',1,7,8,-7),
    ('C11',1,8,8,-6),
    ('C8',1,9,5,-6),
    ('D8',1,10,4,-5),
    ('C7',1,11,4,-6),
    ('D7',1,12,3,-5),
    ('A7',1,13,6,-8),
    ('B7',1,14,5,-7),
    ('A6',1,15,5,-8),
    ('B6',1,16,4,-7),
    ('F13',2,1,7,-3),
    ('F14',2,2,8,-3),
    ('G15',2,3,8,-2),
    ('H16',2,4,8,-1),
    ('G14',2,5,7,-2),
    ('H13',2,6,5,-1),
    ('H14',2,7,6,-1),
    ('H15',2,8,7,-1),
    ('G13',2,9,6,-2),
    ('F12',2,10,6,-3),
    ('E11',2,11,6,-4),
    ('D10',2,12,6,-5),
    ('E13',2,13,8,-4),
    ('E12',2,14,7,-4),
    ('D12',2,15,8,-5),
    ('D11',2,16,7,-5),
    ('M13',3,1,4,4),
    ('M12',3,2,3,4),
    ('N12',3,3,3,5),
    ('N11',3,4,2,5),
    ('K13',3,5,4,2),
    ('L12',3,6,3,3),
    ('M11',3,7,2,4),
    ('N10',3,8,1,5),
    ('K14',3,9,5,2),
    ('J13',3,10,4,1),
    ('J14',3,11,5,1),
    ('J15',3,12,6,1),
    ('L13',3,13,4,3),
    ('L14',3,14,5,3),
    ('K15',3,15,6,2),
    ('J16',3,16,7,1),
    ('Q7',4,1,-2,8),
    ('P7',4,2,-2,7),
    ('Q6',4,3,-3,8),
    ('P6',4,4,-3,7),
    ('O8',4,5,-1,6),
    ('N8',4,6,-1,5),
    ('O7',4,7,-2,6),
    ('N7',4,8,-2,5),
    ('O9',4,9,0,6),
    ('O10',4,10,1,6),
    ('P10',4,11,1,7),
    ('O11',4,12,2,6),
    ('P8',4,13,-1,7),
    ('Q8',4,14,-1,8),
    ('P9',4,15,0,7),
    ('Q9',4,16,0,8),
    ('P3',5,1,-6,7),
    ('Q2',5,2,-7,8),
    ('P2',5,3,-7,7),
    ('Q1',5,4,-8,8),
    ('O3',5,5,-6,6),
    ('O2',5,6,-7,6),
    ('P1',5,7,-8,7),
    ('O1',5,8,-8,6),
    ('O4',5,9,-5,6),
    ('N5',5,10,-4,5),
    ('O5',5,11,-4,6),
    ('N6',5,12,-3,5),
    ('Q3',5,13,-6,8),
    ('P4',5,14,-5,7),
    ('Q4',5,15,-5,8),
    ('P5',5,16,-4,7),
    ('L2',6,1,-7,3),
    ('L1',6,2,-8,3),
    ('K1',6,3,-8,2),
    ('J1',6,4,-8,1),
    ('K2',6,5,-7,2),
    ('J4',6,6,-5,1),
    ('J3',6,7,-6,1),
    ('J2',6,8,-7,1),
    ('K3',6,9,-6,2),
    ('L3',6,10,-6,3),
    ('M3',6,11,-6,4),
    ('N3',6,12,-6,5),
    ('M1',6,13,-8,4),
    ('M2',6,14,-7,4),
    ('N1',6,15,-8,5),
    ('N2',6,16,-7,5),
    ('E1',7,1,-4,-4),
    ('E2',7,2,-3,-4),
    ('D1',7,3,-3,-5),
    ('D2',7,4,-2,-5),
    ('G3',7,5,-4,-2),
    ('F3',7,6,-3,-3),
    ('E3',7,7,-2,-4),
    ('D3',7,8,-1,-5),
    ('G2',7,9,-5,-2),
    ('H4',7,10,-4,-1),
    ('H3',7,11,-5,-1),
    ('H2',7,12,-6,-1),
    ('F2',7,13,-4,-3),
    ('F1',7,14,-5,-3),
    ('G1',7,15,-6,-2),
    ('H1',7,16,-7,-1),
    ('A3',8,1,2,-8),
    ('B4',8,2,2,-7),
    ('A4',8,3,3,-8),
    ('B5',8,4,3,-7),
    ('C4',8,5,1,-6),
    ('D5',8,6,1,-5),
    ('C5',8,7,2,-6),
    ('D6',8,8,2,-5),
    ('C3',8,9,0,-6),
    ('C2',8,10,-1,-6),
    ('B1',8,11,-1,-7),
    ('C1',8,12,-2,-6),
    ('B3',8,13,1,-7),
    ('A2',8,14,1,-8),
    ('B2',8,15,0,-7),
    ('A1',8,16,0,-8),
    ('F10',9,1,4,-3),
    ('F11',9,2,5,-3),
    ('G12',9,3,5,-2),
    ('G10',9,5,3,-2),
    ('G11',9,6,4,-2),
    ('H11',9,7,3,-1),
    ('H12',9,8,4,-1),
    ('F9',9,9,3,-3),
    ('F8',9,10,2,-3),
    ('E8',9,11,3,-4),
    ('E10',9,13,5,-4),
    ('E9',9,14,4,-4),
    ('D9',9,15,5,-5),
    ('M10',10,1,1,4),
    ('M9',10,2,0,4),
    ('N9',10,3,0,5),
    ('L9',10,5,0,3),
    ('L8',10,6,-1,3),
    ('M8',10,7,-1,4),
    ('K10',10,9,1,2),
    ('K11',10,10,2,2),
    ('J11',10,11,2,1),
    ('J12',10,12,3,1),
    ('L10',10,13,1,3),
    ('L11',10,14,2,3),
    ('K12',10,15,3,2),
    ('L5',11,1,-4,3),
    ('L4',11,2,-5,3),
    ('K4',11,3,-5,2),
    ('K6',11,5,-3,2),
    ('K5',11,6,-4,2),
    ('J6',11,7,-3,1),
    ('J5',11,8,-4,1),
    ('L6',11,9,-3,3),
    ('L7',11,10,-2,3),
    ('M6',11,11,-3,4),
    ('M4',11,13,-5,4),
    ('M5',11,14,-4,4),
    ('N4',11,15,-5,5),
    ('E4',12,1,-1,-4),
    ('E5',12,2,0,-4),
    ('D4',12,3,0,-5),
    ('F6',12,5,0,-3),
    ('F7',12,6,1,-3),
    ('E6',12,7,1,-4),
    ('G6',12,9,-1,-2),
    ('G5',12,10,-2,-2),
    ('H6',12,11,-2,-1),
    ('H5',12,12,-3,-1),
    ('F5',12,13,-1,-3),
    ('F4',12,14,-2,-3),
    ('G4',12,15,-3,-2),
)

def layout_positions():
    """설치180곳의주소·격자좌표·길이정보를반환합니다.

    x,y use triangular-lattice pitch1; physical plane pitch is12. C-port numbers
    are the PCB photo's C labels identifying adjacent J1..J16 outputs.
    """
    return [dict(position_id=pid,zone=z,port_c=p,row=pid[0],q=q,r=r,
                 x=q+r/2,y=-r*math.sqrt(3)/2,
                 led_count=ROW_PHYSICAL_COUNTS[pid[0]],
                 visible_led_count=ROW_PHYSICAL_COUNTS[pid[0]]-1)
            for pid,z,p,q,r in _SITES]


def address(position_id):
    """A6같은고정위치ID -> 입력배열의0-based (Zone,C-port) 인덱스."""
    for pid,z,p,_,_ in _SITES:
        if pid==position_id:return z-1,p-1
    raise KeyError(f'Unknown/absent strip: {position_id}')


def read_lengths():
    """내장스냅샷에서물리길이[Zone,C-port]를만듭니다. 파일I/O가없습니다."""
    lengths=np.zeros((12,16),np.uint16)
    for pid,z,p,_,_ in _SITES:lengths[z-1,p-1]=ROW_PHYSICAL_COUNTS[pid[0]]
    if np.count_nonzero(lengths)!=180 or int(lengths.sum())!=8640:
        raise ValueError('Embedded layout is inconsistent')
    return lengths


PHYSICAL_LENGTHS=read_lengths()
VISIBLE_LENGTHS=np.maximum(PHYSICAL_LENGTHS.astype(int)-1,0)
VALID_MASK=np.arange(55)[None,None,:] < VISIBLE_LENGTHS[...,None]
for _array in (PHYSICAL_LENGTHS,VISIBLE_LENGTHS,VALID_MASK):_array.flags.writeable=False


def _rgb_frame(frame):
    rgb=as_rgb(frame)
    if rgb.shape!=SHAPE+(3,):raise ValueError('Expected (12,16,55) or (12,16,55,3)')
    return rgb


def _sequence(value):
    if isinstance(value,(bool,np.bool_)) or not isinstance(value,(int,np.integer)) or not 0<=value<=0xffffffff:
        raise ValueError('sequence must be uint32')
    return int(value)


def as_rgb(frame):
    """uint16 tensors are packed RGB565; other inputs are RGB/brightness. Return RGB8."""
    a = np.asarray(frame)
    if not (a.ndim in (3, 4) and a.shape[:2] == (12, 16)
            and 1 <= a.shape[2] <= 55 and (a.ndim == 3 or a.shape[3] == 3)):
        raise ValueError("Expected (12,16,N) or (12,16,N,3), 1 <= N <= 55")
    if a.ndim == 3 and a.dtype.kind == 'u' and a.dtype.itemsize == 2:
        # A uint16 tensor is already packed RGB565, not a brightness tensor.
        return rgb565_unpack(a)
    if a.dtype.kind not in "buif" or not np.isfinite(a).all() or a.min() < 0:
        raise ValueError("Brightness/RGB values must be finite nonnegative numbers")
    if a.dtype.kind in "bf":
        if a.max() > 1:
            raise ValueError("Float values must be 0..1")
        a = np.rint(a.astype(float) * 255).astype(np.uint8)
    else:
        if a.max() > 255:
            raise ValueError("Integer values must be 0..255")
        a = a.astype(np.uint8)
    if a.ndim == 3:
        a = np.repeat(a[..., None], 3, axis=-1)
    return np.ascontiguousarray(a)



def map_zones(rgb, lengths, zone_to_unit=tuple(range(12))):
    """zone_to_unit[z] is DD index 0..11. Input pixel 0 is physical LED 2."""
    mapping = np.asarray(zone_to_unit)
    if mapping.shape != (12,) or mapping.dtype.kind not in "iu" or set(mapping.tolist()) != set(range(12)):
        raise ValueError("zone_to_unit must be a permutation of integers 0..11")
    if rgb.shape[2] < int(lengths.max())-1:
        raise ValueError("Input N is too short for the layout; use N=55")
    result = np.zeros_like(rgb)
    for z, unit in enumerate(mapping):
        for p, n in enumerate(lengths[z]):
            result[unit, p, :max(0,int(n)-1)] = rgb[z, p, :max(0,int(n)-1)]
    return result


def rgb565_pack(rgb):
    """Prevalidated RGB uint8 -> little-endian uint16; 5 red, 6 green, 5 blue bits."""
    a=rgb.astype(np.uint16)
    return (((a[...,0]>>3)<<11) | ((a[...,1]>>2)<<5) | (a[...,2]>>3)).astype('<u2')


def rgb565_unpack(packed):
    a=np.asarray(packed,dtype=np.uint16)
    r=a>>11;g=(a>>5)&63;b=a&31
    return np.stack(((r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)),axis=-1).astype(np.uint8)



class LEDPowerWarning(UserWarning):
    """A power-related condition needs attention; no current sensing is implied."""


def _level(value, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or not 0 <= value <= 255:
        raise ValueError(f'{name} must be an integer in 0..255')
    return int(value)


class PinMaskEncoder:
    """Reusable configuration; warnings are emitted once per encoder.

    Input axis 0 is Zone, axis 1 is C-port, j=0 is physical LED 2.
    Output embeds the black first physical pixel and row-dependent padding.
    brightness is clamped to max_brightness; both default to 32/255.
    """
    def __init__(self, *, brightness=32, max_brightness=DEFAULT_MAX_BRIGHTNESS,
                 zone_to_unit=tuple(range(12))):
        self._brightness = _level(brightness, 'brightness')
        self._max_brightness = _level(max_brightness, 'max_brightness')
        mapping = np.asarray(zone_to_unit)
        if mapping.shape != (12,) or mapping.dtype.kind not in 'iu' or set(mapping.tolist()) != set(range(12)):
            raise ValueError('zone_to_unit must be a permutation of integers 0..11')
        self.zone_to_unit = tuple(map(int, mapping))
        self.lengths = read_lengths()
        self.lengths.flags.writeable = False
        self._valid = np.arange(55)[None,None,:] < np.maximum(self.lengths.astype(int)-1,0)[...,None]
        self._warned_high = self._warned_white = False

    @property
    def max_brightness(self):
        return self._max_brightness

    @property
    def effective_brightness(self):
        return min(self._brightness, self._max_brightness)

    def preview_frame(self, frame, *, brightness=None):
        """Return actual scaled RGB8 in Zone order, with unused sites black.

        This is already brightness-scaled. Do NOT apply brightness again in GUI.
        Does not mutate the caller's frame or add RGB565 quantization to RGB8.
        """
        rgb = as_rgb(frame)
        if rgb.shape != SHAPE + (3,):
            raise ValueError('Expected 12 x 16 x 55 visible pixels, with optional RGB axis')
        requested = self._brightness if brightness is None else _level(brightness, 'brightness')
        effective = min(requested, self._max_brightness)
        rgb = map_zones(rgb, self.lengths)
        if effective and np.any(rgb):
            if effective > DEFAULT_MAX_BRIGHTNESS and not self._warned_high:
                warnings.warn('밝기 32보다 높은 출력입니다. PC 핀 마스크는 보드의 FastLED 밝기 제한을 우회합니다. '
                              '전원·배선·분기 보호 용량을 확인하세요. 밝기는 전류 제한이 아닙니다.',
                              LEDPowerWarning, stacklevel=2)
                self._warned_high = True
            if not self._warned_white and np.all(rgb[self._valid] == 255):
                warnings.warn('전체 8,460개 가시 LED의 흰색 점등입니다. 밝기 32에서도 전력 소모가 클 수 있습니다. '
                              'MUX는 전원을 나누지 않으며 모든 스트립이 마지막 색으로 계속 켜집니다.',
                              LEDPowerWarning, stacklevel=2)
                self._warned_white = True
        # FastLED scale8 FIXED: verified against all 256 values at brightness 32.
        return ((rgb.astype(np.uint16) * (effective + 1)) >> 8).astype(np.uint8)

    def physical_rgb(self, frame, *, brightness=None):
        scaled = self.preview_frame(frame, brightness=brightness)
        physical = np.zeros(PHYSICAL_SHAPE, np.uint8)
        for zone, unit in enumerate(self.zone_to_unit):
            physical[:,unit,1:,:] = scaled[zone]
        return physical

    def encode_payload(self, frame, *, brightness=None):
        """Return 32,256 B, lossless 12-bit lane masks packed two per 3 bytes."""
        physical = self.physical_rgb(frame, brightness=brightness)
        # RGB를WS2812의GRB순서로바꿉니다.
        grb = physical[...,[1,0,2]]
        bits = np.unpackbits(grb, axis=-1, bitorder='big').reshape(16,12,56,3,8)
        # lane0 -> bit0, ... lane11 -> bit11: one mask controls 12 physical pins.
        weights = (1 << np.arange(12,dtype=np.uint16))[None,:,None,None,None]
        planes = np.sum(bits.astype(np.uint16)*weights, axis=1, dtype=np.uint16)
        # Two 12-bit masks -> three bytes. No color bits are discarded here.
        pairs = planes.reshape(-1,2)
        packed = np.empty((len(pairs),3), np.uint8)
        packed[:,0] = pairs[:,0] & 255
        packed[:,1] = (pairs[:,0] >> 8) | ((pairs[:,1] & 15) << 4)
        packed[:,2] = pairs[:,1] >> 4
        return packed.tobytes()

    def encode_packet(self, frame, sequence, *, brightness=None):
        if isinstance(sequence, (bool,np.bool_)) or not isinstance(sequence, (int,np.integer)) or not 0 <= sequence <= 0xffffffff:
            raise ValueError('sequence must be uint32')
        payload = self.encode_payload(frame, brightness=brightness)
        return HEADER.pack(b'LED3',int(sequence),55,11,len(payload),zlib.crc32(payload))+payload


class RGB565Encoder:
    """RGB8/밝기/RGB565 -> LED2. 밝기는보드에서32를적용합니다.

    encode_payload: 입력검사->유효LED마스킹->Zone/DD매핑->RGB565little-endian.
    RGB8정밀도는R5/G6/B5로양자화됩니다. 첫검정물리픽셀은보드가삽입합니다.
    """
    def __init__(self,*,zone_to_unit=tuple(range(12))):
        self.lengths=read_lengths();self.zone_to_unit=tuple(zone_to_unit)
        map_zones(np.zeros(SHAPE+(3,),np.uint8),self.lengths,self.zone_to_unit)
        self._warned_white=False

    def _masked(self,frame):
        rgb=map_zones(_rgb_frame(frame),self.lengths)
        if not self._warned_white and np.all(rgb[VALID_MASK]==255):
            warnings.warn('전체8,460개가시LED의흰색점등입니다. 보드밝기32도총전류제한이아닙니다. '
                          'MUX는전력분산장치가아니므로전원·배선용량을확인하세요.',LEDPowerWarning,stacklevel=3)
            self._warned_white=True
        return rgb

    def encode_payload(self,frame):
        rgb=self._masked(frame)
        hardware=map_zones(rgb,self.lengths,self.zone_to_unit)
        return rgb565_pack(hardware).tobytes(order='C')

    def encode_packet(self,frame,sequence):
        payload=self.encode_payload(frame)
        return HEADER.pack(b'LED2',_sequence(sequence),55,6,len(payload),zlib.crc32(payload))+payload

    def preview_frame(self,frame):
        """Zone 순서, 밝기 32가 적용된 RGB8. 표시할 때 추가 밝기를 곱하지 마세요."""
        rgb=rgb565_unpack(rgb565_pack(self._masked(frame)))
        return ((rgb.astype(np.uint16)*33)>>8).astype(np.uint8)


class LEDUSB:
    """USB: one writer + one ACK consumer, two-frame window, strict seq/CRC.

    Low-level close() does not turn lights OFF. LiveCentral closes with a black-frame ACK.
    The constructor probes with a black frame; mismatched firmware is rejected.
    """
    READY_PREFIX = b"READY LED2"
    def __init__(self,port,*,version='rgb565',brightness=32,max_brightness=32,
                 zone_to_unit=tuple(range(12)),timeout=10.0,baudrate=115200):
        try:import serial
        except ImportError as exc:raise ImportError('USB송신은 pip install pyserial 이필요합니다') from exc
        if version=='rgb565':
            if brightness!=32 or max_brightness!=32:
                raise ValueError('RGB565 board brightness is fixed at 32; reduce input RGB for dimmer patterns')
            from ripples.hardware.rgb565 import InstallationEncoder
            self.encoder=InstallationEncoder(zone_to_unit=zone_to_unit);self.READY_PREFIX=b'READY LED2'
        elif version=='pinmask':
            self.encoder=PinMaskEncoder(brightness=brightness,max_brightness=max_brightness,zone_to_unit=zone_to_unit)
            self.READY_PREFIX=b'READY LED3'
        else:raise ValueError("version must be 'rgb565' or 'pinmask'")
        self.write_chunk = 64 if version == "rgb565" and sys.platform == "win32" else 4096
        self.sequence = 0
        self.inflight = {}
        self.inflight_lock = threading.Lock()
        self._status = bytearray()
        self.timeout = timeout
        self.serial = serial.Serial(port, baudrate=baudrate, timeout=0.1, write_timeout=timeout)
        # Open may reset the Nano; if READY was already printed, no reset is required.
        # First send also waits for ACK, so a missed READY does not break the session.
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                status=self.serial.readline()
                if status.startswith(self.READY_PREFIX):break
                if status.startswith(b'READY '):
                    raise RuntimeError(f'Firmware mismatch: expected {self.READY_PREFIX!r}, got {status!r}')
            self.send(np.zeros(SHAPE,np.uint8))  # BLACK-only compatibility probe.

        except BaseException:
            self.close()
            raise

    def preview_frame(self,frame):
        return self.encoder.preview_frame(frame)

    def submit(self, frame):
        """Queue at most two frames; returns sequence, not a display-complete ACK."""
        sequence = self.sequence
        data = self.encoder.encode_packet(frame,sequence)
        written = threading.Event()
        meta = {"sequence": sequence, "sent_at": time.perf_counter(),
                "crc": zlib.crc32(data[HEADER.size:]), "_written":written}
        with self.inflight_lock:
            if len(self.inflight) >= 2:
                raise RuntimeError("Two-frame window full: call wait_next() before submit()")
            self.inflight[sequence] = meta
            self.sequence = (sequence + 1) & 0xFFFFFFFF
        try:
            for start in range(0, len(data), self.write_chunk):
                chunk = data[start:start+self.write_chunk]
                if self.serial.write(chunk) != len(chunk):
                    raise OSError("Incomplete serial write")
            # flush() means host write finished, NOT that the LEDs displayed the frame.
            self.serial.flush()
            meta["write_ms"] = (time.perf_counter()-meta["sent_at"])*1000
            written.set()
            return sequence
        except BaseException:
            self.close()
            raise

    def wait_next(self):
        """Wait for the oldest full 16-slot display completion, validating sequence/CRC."""
        with self.inflight_lock:
            if not self.inflight:
                raise RuntimeError("No pending frames")
            oldest = next(iter(self.inflight))
        deadline = time.monotonic() + self.timeout
        try:
            while time.monotonic() < deadline:
                raw = self.serial.readline()
                if not raw:
                    continue
                # readline() may time out mid-line: retain incomplete status bytes.
                self._status.extend(raw)
                while b"\n" in self._status:
                    line, _, remainder = self._status.partition(b"\n")
                    self._status = bytearray(remainder)
                    parts = line.decode(errors="replace").strip().split()
                    if not parts: continue
                    if parts[0] == "ERR":
                        raise RuntimeError(" ".join(parts))
                    if parts[0] != "OK":
                        continue  # RX is acceptance, never display completion.
                    sequence = int(parts[1])
                    if sequence != oldest:
                        raise RuntimeError(f"Out-of-order ACK: expected {oldest}, got {sequence}")
                    arrived = time.perf_counter()
                    with self.inflight_lock:
                        meta = self.inflight[sequence]
                    if not meta['_written'].wait(self.timeout):
                        raise TimeoutError('Writer did not finish')
                    meta.pop('_written')
                    with self.inflight_lock:
                        self.inflight.pop(sequence)
                    meta["host_rtt_ms"] = (arrived-meta["sent_at"])*1000
                    meta["completed_at"] = arrived
                    if len(parts) != 8:
                        raise RuntimeError("Expected full sequence/timing/CRC ACK")
                    if len(parts) == 8:
                        meta.update(rx_us=int(parts[2]), scan_us=int(parts[3]),
                                    start_us=int(parts[4]), end_us=int(parts[5]),
                                    rx_end_us=int(parts[6]), board_crc=int(parts[7],16))
                        if meta["board_crc"] != meta["crc"]:
                            raise RuntimeError("MCU payload CRC differs from the submitted frame")
                    return meta
            raise TimeoutError(f"No display ACK for frame {oldest}; reconnect before retrying")
        except BaseException:
            self.close()
            raise

    def send(self, frame):
        """Compatible synchronous API. For overlap use submit()/wait_next()."""
        if self.inflight:
            raise RuntimeError("Drain pending submit() frames before synchronous send()")
        self.submit(frame)
        return self.wait_next()

    def close(self):
        self.serial.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

class CentralUSB(LEDUSB):
    """RGB565 LED2 firmware. Same send/submit/wait_next API as PinMaskUSB."""
    def __init__(self,port,**options):super().__init__(port,version='rgb565',**options)


class PinMaskUSB(LEDUSB):
    """PC-mask LED3 firmware. PC brightness/cap default to 32; there is no board-side cap."""
    def __init__(self,port,**options):super().__init__(port,version='pinmask',**options)


class LiveCentral:
    """Separate USB writer/ACK reader; producers copy into a one-frame mailbox.

    max two frames in flight on USB/MCU, one newer unsent frame on the PC.
    Updates to that unsent frame replace it instead of accumulating latency.
    All actual GUI drawing must remain on the GUI/main thread.
    """
    def __init__(self, port, *, window=2, transport_factory=CentralUSB, **options):
        if window not in (1,2):raise ValueError('window must be 1 or 2')
        self.window=window
        self.transport_factory=transport_factory
        self.port,self.options=port,options
        self.condition=threading.Condition()
        self.pending=None
        self.displayed=None
        self.error=None
        self.stop=False
        self.dropped=0
        self.sent=0
        self.completed=0
        self.first_end_us=None
        self.last_end_us=None
        self.thread=threading.Thread(target=self._run,name='LED_USB',daemon=True)
        self.thread.start()

    def publish(self, frame):
        snapshot=np.array(frame,copy=True)  # Producer may mutate its original immediately.
        with self.condition:
            if self.error: raise RuntimeError('USB worker failed') from self.error
            if self.stop: raise RuntimeError('Stream is closed')
            if self.pending is not None:self.dropped+=1
            self.pending=snapshot
            self.condition.notify_all()

    def poll_displayed(self):
        """Main thread: return latest (ACK, original Zone frame) once, or None."""
        with self.condition:
            if self.error: raise RuntimeError('USB worker failed') from self.error
            item=self.displayed;self.displayed=None
            return item

    def _run(self):
        saved={}
        stop_reader=threading.Event()
        reader=None
        try:
            with self.transport_factory(self.port,**self.options) as link:
                def receive_acks():
                    try:
                        while not stop_reader.is_set():
                            with self.condition:
                                if not link.inflight:
                                    self.condition.wait(.01)
                                    continue
                            ack=link.wait_next()
                            with self.condition:
                                original=saved.pop(ack['sequence'])
                                self.displayed=(ack,original)
                                self.completed+=1
                                if 'end_us' in ack:
                                    if self.first_end_us is None:self.first_end_us=ack['end_us']
                                    self.last_end_us=ack['end_us']
                                self.condition.notify_all()
                    except BaseException as e:
                        with self.condition:
                            self.error=e;self.stop=True;self.condition.notify_all()
                reader=threading.Thread(target=receive_acks,name='LED_ACK',daemon=True)
                reader.start()
                try:
                    while True:
                        with self.condition:
                            while not self.stop and (self.pending is None or len(link.inflight)>=self.window):
                                self.condition.wait(.02)
                            if self.error:raise RuntimeError('ACK receiver failed') from self.error
                            if self.stop:
                                self.pending=None
                                break
                            frame=self.pending;self.pending=None
                            # Register the immutable snapshot BEFORE USB bytes can arrive.
                            saved[link.sequence]=link.preview_frame(frame)
                        link.submit(frame)
                        with self.condition:
                            self.sent+=1;self.condition.notify_all()
                    with self.condition:
                        while link.inflight and not self.error:
                            self.condition.wait(.05)
                        if self.error:raise RuntimeError('ACK receiver failed') from self.error
                    stop_reader.set()
                    with self.condition:self.condition.notify_all()
                    reader.join(1)
                    if reader.is_alive():raise TimeoutError('ACK reader did not stop')
                    link.send(np.zeros((12,16,55,3),np.uint8))
                finally:
                    stop_reader.set()
                    with self.condition:self.condition.notify_all()
                    if reader:reader.join(1)
        except BaseException as e:
            with self.condition:
                self.error=e;self.condition.notify_all()

    def close(self):
        with self.condition:
            self.stop=True;self.condition.notify_all()
        self.thread.join(timeout=35)
        if self.thread.is_alive():raise TimeoutError('USB worker did not finish')
        if self.error:raise RuntimeError('USB worker failed') from self.error

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


def self_test():
    """Offline checks: no USB is opened and nothing is displayed. Raises on failure."""
    assert len(_SITES)==180 and len({x[0] for x in _SITES})==180
    assert len({(x[1],x[2]) for x in _SITES})==180
    assert int(PHYSICAL_LENGTHS.sum())==8640 and int(VALID_MASK.sum())==8460
    assert address('A6')==(0,14)
    f=np.zeros(SHAPE+(3,),np.uint8);z,p=address('A6');f[z,p,0]=[255,0,0]
    for encoder,magic,flags,length in [(RGB565Encoder(),b'LED2',6,21120),
                                       (PinMaskEncoder(),b'LED3',11,32256)]:
        data=encoder.encode_packet(f,17)
        m,seq,n,fl,size,crc=HEADER.unpack(data[:20])
        assert (m,seq,n,fl,size)==(magic,17,55,flags,length)
        assert zlib.crc32(data[20:])==crc and len(data)==20+length
        np.testing.assert_array_equal(encoder.preview_frame(f)[z,p,0],[32,0,0])
    physical=PinMaskEncoder().physical_rgb(f)
    assert not physical[:,:,0].any() and np.count_nonzero(physical)==1
    # Independently decode every 12-bit mask back to 12-lane RGB bytes.
    b=np.frombuffer(PinMaskEncoder().encode_payload(f),np.uint8).astype(np.uint16).reshape(-1,3)
    masks=np.empty((len(b),2),np.uint16)
    masks[:,0]=b[:,0]|((b[:,1]&15)<<8);masks[:,1]=(b[:,1]>>4)|(b[:,2]<<4)
    masks=masks.reshape(16,56,3,8)
    actual=np.zeros(PHYSICAL_SHAPE,np.uint8)
    for lane in range(12):
        for bit in range(8):actual[:,lane]|=((((masks>>lane)&1)[...,bit])<<(7-bit)).astype(np.uint8)
    np.testing.assert_array_equal(actual[...,[1,0,2]],physical)
    rng=np.random.default_rng(713);rgb=rng.integers(0,256,SHAPE+(3,),np.uint8)
    original=rgb.copy()
    PinMaskEncoder(brightness=255).encode_packet(rgb,0)
    np.testing.assert_array_equal(rgb,original)
    assert PinMaskEncoder(brightness=255).effective_brightness==32
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter('always');white=np.full(SHAPE+(3,),255,np.uint8)
        assert PinMaskEncoder(brightness=255).preview_frame(white).max()==32
        assert seen and all(w.category is LEDPowerWarning for w in seen)
    words=np.arange(65536,dtype=np.uint16)
    np.testing.assert_array_equal(rgb565_pack(rgb565_unpack(words)),words)
    print('PASS: embedded180strips,8640physical/8460visible, bothpacketCRC/layout, '
          'hiddenfirstLED,12-bitmaskdecode,brightnesscap/warning,RGB565roundtrip. NoUSBopened.')


def main():
    parser=argparse.ArgumentParser(description='Single-file LED2/LED3 handoff. No automatic light pattern.')
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--version',choices=('rgb565','pinmask'),default='rgb565')
    parser.add_argument('--file',type=Path,help='Input .npy, allow_pickle=False')
    parser.add_argument('--output',type=Path,help='Write a complete binary packet; no USB')
    parser.add_argument('--port',help='Send one frame and wait for ACK; firmware must match version')
    parser.add_argument('--brightness',type=int,default=32)
    parser.add_argument('--max-brightness',type=int,default=32)
    args=parser.parse_args()
    if args.self_test:self_test();return
    if args.file is None or bool(args.port)==bool(args.output):
        parser.error('Provide --file plus exactly one of --output/--port, or --self-test')
    options=dict(brightness=args.brightness,max_brightness=args.max_brightness)
    if args.version=='rgb565' and options!={'brightness':32,'max_brightness':32}:
        parser.error('RGB565 board brightness is fixed at 32; reduce input RGB for dimmer patterns')
    frame=np.load(args.file,allow_pickle=False)
    if args.output:
        encoder=RGB565Encoder() if args.version=='rgb565' else PinMaskEncoder(**options)
        args.output.write_bytes(encoder.encode_packet(frame,0))
        print(f'Wrote {args.output}; version={args.version}; noUSBopened')
    else:
        from board_check import confirm_board
        confirm_board(args.port, args.version)
        factory=CentralUSB if args.version=='rgb565' else PinMaskUSB
        with factory(args.port,**options) as link:
            ack=link.send(frame);print(f'Complete seq={ack["sequence"]}, CRCverified')
        print('The last frame is still latched. Send a zero frame to explicitly turn OFF.')


if __name__=='__main__':main()
