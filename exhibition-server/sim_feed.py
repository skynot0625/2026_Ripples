"""심 LED frames from outside (AI) for the /sim simulator (2026-11-01).

One frame = numpy uint8 array (12, 16, 64, 3) as bytes (C order, 36,864 bytes):
12 submodules (zone 1–12), 16 ports (1–16), 64 LEDs from the top (LED 1 in the hole is not included), RGB.
Ports with no strip and LEDs past the end of a strip are ignored (fill them with 0).
The server keeps only the latest frame; /sim draws it through the port map in sim.html.
"""

from __future__ import annotations

import threading
import time
from collections import deque

FRAME_SHAPE = (12, 16, 64, 3)
FRAME_BYTES = 12 * 16 * 64 * 3


class SimFeed:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.frame: bytes | None = None
        self.version = 0
        self.times: deque[float] = deque(maxlen=64)
        self.source = ""

    def push(self, data: bytes, source: str) -> None:
        if len(data) != FRAME_BYTES:
            raise ValueError(f"frame must be {FRAME_BYTES} bytes (uint8 {FRAME_SHAPE}), got {len(data)}")
        with self.cond:
            self.frame = bytes(data)
            self.version += 1
            self.times.append(time.time())
            self.source = source
            self.cond.notify_all()

    def wait(self, last: int, timeout: float) -> tuple[int, bytes | None]:
        with self.cond:
            if self.version == last:
                self.cond.wait(timeout)
            return self.version, self.frame

    def status(self) -> dict[str, object]:
        with self.cond:
            now = time.time()
            recent = [t for t in self.times if now - t < 2.0]
            fps = (len(recent) - 1) / (recent[-1] - recent[0]) if len(recent) > 1 and recent[-1] > recent[0] else 0.0
            return {"frames": self.version, "fps": round(fps, 1), "age_s": round(now - self.times[-1], 2) if self.times else None,
                    "source": self.source, "frame_bytes": FRAME_BYTES, "shape": list(FRAME_SHAPE)}
