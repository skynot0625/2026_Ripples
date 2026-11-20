import logging
import queue
import threading
import time
import numpy as np
from .protocol import Decoder, Packet, FRAMES, RATE

LOG = logging.getLogger('capture')

class Capture(threading.Thread):
    """Read USB independently of playback; bounded queue drops oldest for live audio."""
    def __init__(self, stop, port='auto', capacity=24, timeout=5.0, demo=False):
        super().__init__(daemon=True)
        self.stop_event, self.port, self.timeout, self.demo = stop, port, timeout, demo
        self.queue = queue.Queue(maxsize=capacity)
        self.dropped = self.bad_packets = self.reconnects = 0
        self.error = None
        self.generation = 0

    def offer(self, packet):
        item = (time.monotonic(), self.generation, packet)
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            self.queue.put_nowait(item)

    def clear(self):
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break

    def run(self):
        if self.demo:
            seq, deadline = 0, time.monotonic()
            while not self.stop_event.is_set():
                t = (np.arange(FRAMES) + seq * FRAMES) / RATE
                values = np.stack([0.1 * np.sin(2*np.pi*f*t) for f in (440,660,880)], axis=1)
                self.offer(Packet(seq & 0xffffffff, 0, (values * 32767).astype('<i2').tobytes()))
                seq += 1
                deadline += FRAMES / RATE
                self.stop_event.wait(max(0, deadline - time.monotonic()))
            return
        try:
            import serial
            from serial.tools import list_ports
        except ImportError as exc:
            self.error = exc
            self.stop_event.set()
            return
        while not self.stop_event.is_set():
            decoder = Decoder()
            try:
                port = self.port
                if port == 'auto':
                    ports = [p.device for p in list_ports.comports() if p.vid == 0x16c0]
                    if len(ports) != 1:
                        raise RuntimeError('Teensy must be unique; specify --port if needed')
                    port = ports[0]
                with serial.Serial(port, 115200, timeout=0.1, exclusive=True) as stream:
                    LOG.info('USB connected: %s', port)
                    last_valid = time.monotonic()
                    while not self.stop_event.is_set():
                        data = stream.read(min(max(stream.in_waiting, 796), 8192))
                        for packet in decoder.feed(data):
                            self.offer(packet)
                            last_valid = time.monotonic()
                        if time.monotonic() - last_valid > self.timeout:
                            raise RuntimeError('No valid USB audio')
            except (OSError, RuntimeError, serial.SerialException) as exc:
                LOG.warning('USB disconnected/unavailable: %s; retrying', exc)
                self.clear()
                self.generation += 1
                self.reconnects += 1
                self.stop_event.wait(2)
            except Exception as exc:
                self.error = exc
                self.stop_event.set()
            finally:
                self.bad_packets += decoder.bad_packets
