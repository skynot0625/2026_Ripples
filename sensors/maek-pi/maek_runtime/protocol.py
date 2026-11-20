"""Version 1: fixed-size, CRC-protected, three-channel PCM packets."""
import struct
import zlib
from dataclasses import dataclass

MAGIC = b'MIC3PCM\n'
HEADER = struct.Struct('<8sIIHBBI')
FRAMES = 128
RATE = 44100
PAYLOAD_BYTES = FRAMES * 3 * 2
PACKET_BYTES = HEADER.size + PAYLOAD_BYTES + 4

@dataclass
class Packet:
    sequence: int
    dropped: int
    pcm: bytes


def encode(sequence, pcm, dropped=0):
    if len(pcm) != PAYLOAD_BYTES:
        raise ValueError('Expected 128 frames of 3-channel int16 PCM')
    body = HEADER.pack(MAGIC, sequence, RATE, FRAMES, 3, 16, dropped) + pcm
    return body + struct.pack('<I', zlib.crc32(body))


class Decoder:
    def __init__(self):
        self.buffer = bytearray()
        self.bad_packets = 0
        self.discarded_bytes = 0

    def feed(self, data):
        self.buffer.extend(data)
        packets = []
        while True:
            pos = self.buffer.find(MAGIC)
            if pos < 0:
                discard = max(0, len(self.buffer) - len(MAGIC) + 1)
                self.discarded_bytes += discard
                del self.buffer[:discard]
                break
            if pos:
                self.discarded_bytes += pos
                del self.buffer[:pos]
            if len(self.buffer) < HEADER.size:
                break
            _, seq, rate, frames, channels, bits, dropped = HEADER.unpack_from(self.buffer)
            if (rate, frames, channels, bits) != (RATE, FRAMES, 3, 16):
                self.bad_packets += 1
                del self.buffer[0]
                continue
            if len(self.buffer) < PACKET_BYTES:
                break
            body = self.buffer[:PACKET_BYTES - 4]
            crc, = struct.unpack_from('<I', self.buffer, PACKET_BYTES - 4)
            if zlib.crc32(body) != crc:
                self.bad_packets += 1
                del self.buffer[0]
                continue
            packets.append(Packet(seq, dropped, bytes(body[HEADER.size:])))
            del self.buffer[:PACKET_BYTES]
        return packets
