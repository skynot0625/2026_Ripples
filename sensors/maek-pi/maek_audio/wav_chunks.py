"""Package unprocessed, interleaved PCM into exact one-second WAV chunks.

No disk or network I/O occurs here. The caller must queue completed chunks
outside its real-time capture callback.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple
import io
import wave

import numpy as np


@dataclass(frozen=True)
class WavChunk:
    sequence: int
    start_sample_index: int
    start_time_ns: Optional[int]
    frame_count: int
    sample_rate_hz: int
    channel_order: Tuple[str, str, str]
    sample_width_bytes: int
    complete: bool
    wav_bytes: bytes


class PcmWavChunker:
    """Accept contiguous source PCM bytes; emit 1-second, 3-channel WAV files."""

    def __init__(
        self,
        *,
        sample_rate_hz: int,
        channel_order: Tuple[str, str, str],
        sample_width_bytes: int,
        capture_start_time_ns: Optional[int] = None,
    ):
        if sample_rate_hz < 8000:
            raise ValueError("sample_rate_hz must be at least 8000")
        if sorted(channel_order) != ["center", "left", "right"]:
            raise ValueError("channel_order must contain left, center, right once each")
        if sample_width_bytes not in (2, 3, 4):
            raise ValueError("sample_width_bytes must be 2, 3, or 4 for integer PCM")
        self.sample_rate_hz = sample_rate_hz
        self.channel_order = channel_order
        self.sample_width_bytes = sample_width_bytes
        self.capture_start_time_ns = capture_start_time_ns
        self._frame_bytes = 3 * sample_width_bytes
        self._pending = bytearray()
        self._pending_frames = 0
        self._total_frames = 0
        self._sequence = 0

    @property
    def next_sample_index(self):
        return self._total_frames

    def _make_chunk(self, complete):
        count = self._pending_frames
        if count == 0:
            return None
        start = self._total_frames - count
        start_time = None
        if self.capture_start_time_ns is not None:
            start_time = self.capture_start_time_ns + round(start * 1_000_000_000 / self.sample_rate_hz)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(3)
            writer.setsampwidth(self.sample_width_bytes)
            writer.setframerate(self.sample_rate_hz)
            writer.writeframes(self._pending)
        chunk = WavChunk(
            sequence=self._sequence,
            start_sample_index=start,
            start_time_ns=start_time,
            frame_count=count,
            sample_rate_hz=self.sample_rate_hz,
            channel_order=self.channel_order,
            sample_width_bytes=self.sample_width_bytes,
            complete=complete,
            wav_bytes=buffer.getvalue(),
        )
        self._sequence += 1
        self._pending.clear()
        self._pending_frames = 0
        return chunk

    def append_bytes(self, pcm_bytes: bytes, *, first_sample_index: Optional[int] = None) -> List[WavChunk]:
        """PCM must already be signed little-endian and interleaved in channel_order."""
        if first_sample_index is not None and first_sample_index != self._total_frames:
            raise ValueError("discontinuous sample index; do not silently join missing audio")
        raw = memoryview(pcm_bytes)
        if len(raw) % self._frame_bytes:
            raise ValueError("PCM byte count must contain whole three-channel frames")
        completed = []
        offset = 0
        while offset < len(raw):
            remaining_frames = self.sample_rate_hz - self._pending_frames
            available_frames = (len(raw) - offset) // self._frame_bytes
            frames = min(remaining_frames, available_frames)
            next_offset = offset + frames * self._frame_bytes
            self._pending.extend(raw[offset:next_offset])
            self._pending_frames += frames
            self._total_frames += frames
            offset = next_offset
            if self._pending_frames == self.sample_rate_hz:
                completed.append(self._make_chunk(complete=True))
        return completed

    def append_frames(self, pcm_frames, *, first_sample_index: Optional[int] = None) -> List[WavChunk]:
        """Convenience for int16/int32 arrays. Packed 24-bit uses append_bytes."""
        array = np.asarray(pcm_frames)
        if array.ndim != 2 or array.shape[1] != 3:
            raise ValueError("pcm_frames must have shape (frames, 3)")
        expected_dtype = {2: np.dtype("int16"), 4: np.dtype("int32")}.get(self.sample_width_bytes)
        if expected_dtype is None or array.dtype != expected_dtype:
            raise TypeError("array dtype must match 16-bit or 32-bit signed PCM; use append_bytes for 24-bit")
        little_endian = array.astype("<i%d" % self.sample_width_bytes, copy=False)
        return self.append_bytes(little_endian.tobytes(order="C"), first_sample_index=first_sample_index)

    def flush_partial(self) -> Optional[WavChunk]:
        """At shutdown only; returns an explicitly incomplete last WAV chunk."""
        return self._make_chunk(complete=False)
