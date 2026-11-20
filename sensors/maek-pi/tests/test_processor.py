import math
import unittest

import numpy as np

from maek_audio import MaekAudioProcessor, ProcessorConfig


SAMPLE_RATE = 16000
POSITIONS = {
    "left": (-0.10, 0.0),
    "center": (0.0, 0.12),
    "right": (0.10, 0.0),
}


def tone(amplitudes, *, frames=1600, frequency=600):
    time = np.arange(frames) / SAMPLE_RATE
    signal = np.sin(2 * np.pi * frequency * time)
    return np.stack([signal * value for value in amplitudes], axis=1).astype(np.float32)


def delayed_noise(source_xy, *, frames=4096):
    rng = np.random.default_rng(13)
    source = rng.standard_normal(frames).astype(np.float32) * 0.1
    direction = np.asarray(source_xy, dtype=float)
    direction /= np.linalg.norm(direction)
    delays = [-np.dot(np.asarray(POSITIONS[name]), direction) / 343.0 for name in ("left", "center", "right")]
    samples = np.rint((np.asarray(delays) - min(delays)) * SAMPLE_RATE).astype(int)
    channels = []
    for delay in samples:
        channel = np.zeros(frames, dtype=np.float32)
        channel[delay:] = source[:frames - delay]
        channels.append(channel)
    return np.stack(channels, axis=1)


class ProcessorTests(unittest.TestCase):
    def config(self, **overrides):
        values = dict(
            sample_rate_hz=SAMPLE_RATE,
            channel_order=("left", "center", "right"),
            processed_mix=0.0,
            output_gain=1.0,
        )
        values.update(overrides)
        return ProcessorConfig(**values)

    def test_stereo_uses_side_mics_not_center(self):
        processor = MaekAudioProcessor(self.config())
        pcm = np.zeros((256, 3), dtype=np.float32)
        pcm[:, 0] = 0.2
        pcm[:, 1] = 0.6
        pcm[:, 2] = -0.15
        result = processor.process(pcm, sample_rate_hz=SAMPLE_RATE)
        np.testing.assert_allclose(result.stereo_pcm[:, 0], 0.2, atol=1e-6)
        np.testing.assert_allclose(result.stereo_pcm[:, 1], -0.15, atol=1e-6)
        self.assertFalse(result.aec_active)

    def test_haptic_side_front_and_multiple_sources(self):
        left = MaekAudioProcessor(self.config()).process(tone((0.12, 0.0, 0.0)), sample_rate_hz=SAMPLE_RATE)
        right = MaekAudioProcessor(self.config()).process(tone((0.0, 0.0, 0.12)), sample_rate_hz=SAMPLE_RATE)
        front = MaekAudioProcessor(self.config()).process(tone((0.0, 0.12, 0.0)), sample_rate_hz=SAMPLE_RATE)
        both = MaekAudioProcessor(self.config()).process(tone((0.10, 0.0, 0.10)), sample_rate_hz=SAMPLE_RATE)
        self.assertGreater(left.haptic_left, left.haptic_right + 0.1)
        self.assertGreater(right.haptic_right, right.haptic_left + 0.1)
        self.assertAlmostEqual(front.haptic_left, front.haptic_right, places=6)
        self.assertGreater(front.haptic_left, 0)
        self.assertGreater(both.haptic_left, 0)
        self.assertGreater(both.haptic_right, 0)
        self.assertAlmostEqual(both.haptic_left, both.haptic_right, places=6)

    def test_motion_reduces_haptic_and_limiter_binds_audio(self):
        loud = tone((2.0, 2.0, 2.0))
        still = MaekAudioProcessor(self.config()).process(loud, sample_rate_hz=SAMPLE_RATE)
        moving = MaekAudioProcessor(self.config()).process(loud, sample_rate_hz=SAMPLE_RATE, is_moving=True)
        self.assertTrue(still.input_clipped)
        self.assertLessEqual(float(np.max(np.abs(still.stereo_pcm))), 0.8 + 1e-6)
        self.assertLess(moving.haptic_left, still.haptic_left)

    def test_unknown_geometry_does_not_invent_angle(self):
        result = MaekAudioProcessor(self.config()).process(tone((0.1, 0.1, 0.1)), sample_rate_hz=SAMPLE_RATE)
        self.assertIsNone(result.direction_deg)
        self.assertEqual(result.direction_confidence, 0)

    def test_measured_geometry_can_estimate_left_and_right(self):
        cfg = self.config(mic_positions_m=POSITIONS, channels_synchronized=True)
        left = MaekAudioProcessor(cfg).process(delayed_noise((-1, 0)), sample_rate_hz=SAMPLE_RATE)
        right = MaekAudioProcessor(cfg).process(delayed_noise((1, 0)), sample_rate_hz=SAMPLE_RATE)
        self.assertIsNotNone(left.direction_deg)
        self.assertIsNotNone(right.direction_deg)
        self.assertLess(left.direction_deg, -45)
        self.assertGreater(right.direction_deg, 45)
        self.assertGreater(left.direction_confidence, 0.35)

    def test_geometry_without_verified_sync_does_not_emit_angle(self):
        cfg = self.config(mic_positions_m=POSITIONS)
        result = MaekAudioProcessor(cfg).process(delayed_noise((1, 0)), sample_rate_hz=SAMPLE_RATE)
        self.assertIsNone(result.direction_deg)

    def test_stereo_filter_state_matches_continuous_processing(self):
        cfg = self.config(processed_mix=0.35, output_gain=0.5)
        rng = np.random.default_rng(4)
        pcm = (rng.standard_normal((1100, 3)) * 0.08).astype(np.float32)
        whole = MaekAudioProcessor(cfg).process(pcm, sample_rate_hz=SAMPLE_RATE)
        split_processor = MaekAudioProcessor(cfg)
        pieces = [
            split_processor.process(pcm[start:end], sample_rate_hz=SAMPLE_RATE).stereo_pcm
            for start, end in ((0, 80), (80, 377), (377, 1100))
        ]
        np.testing.assert_allclose(np.concatenate(pieces), whole.stereo_pcm, atol=2e-7)

    def test_bad_input_is_rejected_instead_of_guessing_pcm_scale(self):
        processor = MaekAudioProcessor(self.config())
        with self.assertRaises(TypeError):
            processor.process(np.zeros((20, 3), dtype=np.int32), sample_rate_hz=SAMPLE_RATE)
        with self.assertRaises(ValueError):
            processor.process(np.zeros((20, 2), dtype=np.float32), sample_rate_hz=SAMPLE_RATE)
        with self.assertRaises(ValueError):
            processor.process(np.zeros((20, 3), dtype=np.float32), sample_rate_hz=48000)


if __name__ == "__main__":
    unittest.main()
