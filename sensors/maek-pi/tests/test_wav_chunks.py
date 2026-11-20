import io
import unittest
import wave

import numpy as np

from maek_audio import PcmWavChunker


class WavChunkTests(unittest.TestCase):
    def test_exact_one_second_chunks_preserve_source_order(self):
        sample_rate = 8000
        frames = np.empty((sample_rate * 2 + 17, 3), dtype=np.int16)
        frames[:, 0] = np.arange(len(frames), dtype=np.int16)
        frames[:, 1] = -123
        frames[:, 2] = 456
        original = frames.copy()
        chunker = PcmWavChunker(
            sample_rate_hz=sample_rate,
            channel_order=("left", "center", "right"),
            sample_width_bytes=2,
            capture_start_time_ns=1_000_000_000,
        )
        chunks = []
        offset = 0
        for count in (123, 1777, 800, 5300, 3000, 1034):
            part = frames[offset:offset + count]
            chunks.extend(chunker.append_frames(part, first_sample_index=offset))
            offset += len(part)
        if offset < len(frames):
            chunks.extend(chunker.append_frames(frames[offset:], first_sample_index=offset))
        self.assertEqual(len(chunks), 2)
        self.assertEqual([chunk.sequence for chunk in chunks], [0, 1])
        self.assertEqual([chunk.start_sample_index for chunk in chunks], [0, sample_rate])
        self.assertEqual([chunk.start_time_ns for chunk in chunks], [1_000_000_000, 2_000_000_000])
        for index, chunk in enumerate(chunks):
            self.assertTrue(chunk.complete)
            with wave.open(io.BytesIO(chunk.wav_bytes), "rb") as reader:
                self.assertEqual(reader.getnchannels(), 3)
                self.assertEqual(reader.getframerate(), sample_rate)
                self.assertEqual(reader.getsampwidth(), 2)
                self.assertEqual(reader.getnframes(), sample_rate)
                self.assertEqual(reader.readframes(sample_rate), original[index * sample_rate:(index + 1) * sample_rate].tobytes())
        tail = chunker.flush_partial()
        self.assertIsNotNone(tail)
        self.assertFalse(tail.complete)
        self.assertEqual(tail.frame_count, 17)
        np.testing.assert_array_equal(frames, original)

    def test_24_bit_bytes_and_gap_detection(self):
        chunker = PcmWavChunker(
            sample_rate_hz=8000,
            channel_order=("center", "left", "right"),
            sample_width_bytes=3,
        )
        with self.assertRaises(ValueError):
            chunker.append_bytes(b"x" * 8)
        raw = b"\x01\x02\x03" * 3 * 8000
        chunk = chunker.append_bytes(raw, first_sample_index=0)[0]
        with wave.open(io.BytesIO(chunk.wav_bytes), "rb") as reader:
            self.assertEqual(reader.getsampwidth(), 3)
            self.assertEqual(reader.readframes(8000), raw)
        with self.assertRaises(ValueError):
            chunker.append_bytes(raw[:9], first_sample_index=8002)


if __name__ == "__main__":
    unittest.main()
