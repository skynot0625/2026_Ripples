import unittest

import numpy as np
from maek_runtime.smooth import Declicker, DriftController, DriftResampler

RATE = 44100


def sine(start, frames, freq=440.0, amp=0.5):
    t = (np.arange(frames) + start) / RATE
    return np.stack([amp * np.sin(2 * np.pi * freq * t)] * 2, axis=1).astype(np.float32)


def max_step(blocks):
    signal = np.concatenate(blocks)[:, 0]
    return float(np.abs(np.diff(signal)).max())


class SmoothTests(unittest.TestCase):
    smooth_limit = 0.5 * 2 * np.pi * 440 / RATE * 1.05  # steepest slope of the sine itself

    def test_gap_is_faded_not_jumped(self):
        declick, out, start = Declicker(), [], 0
        for index in range(40):
            if index == 20:
                start += 128 * 3  # three packets lost
            out += declick.push(sine(start, 128), gap=(index == 20))
            start += 128
        out += declick.fade_out()
        # Without fades the jump at the gap would be up to ~1.0.
        self.assertLess(max_step(out), 0.045)  # sine slope 0.031 + fade slope 0.006
        self.assertEqual(declick.fades, 2)

    def test_continuous_stream_unchanged_after_fade_in(self):
        declick, out = Declicker(), []
        blocks = [sine(i * 128, 128) for i in range(10)]
        for block in blocks:
            out += declick.push(block)
        np.testing.assert_array_equal(np.concatenate(out[1:]), np.concatenate(blocks[1:9]))

    def test_resampler_identity_and_seamless_at_drift(self):
        resampler = DriftResampler()
        blocks = [sine(i * 128, 128) for i in range(50)]
        same = np.concatenate([resampler.process(b, 1.0) for b in blocks])
        np.testing.assert_allclose(same[1:], np.concatenate(blocks)[:-1], atol=1e-6)
        resampler = DriftResampler()
        out = [resampler.process(b, 1.004) for b in blocks]
        total = sum(len(b) for b in out)
        self.assertAlmostEqual(total / (50 * 128), 1.004, places=3)
        self.assertLess(max_step(out), self.smooth_limit)

    def test_controller_tracks_fast_and_slow_mic_clock(self):
        # Mic clock 400 ppm fast/slow against a speaker paced at exactly 44100 Hz.
        for ppm in (400, -400):
            drift, resampler = DriftController(), DriftResampler()
            queue_level, sink_frames, produced_in = 6.0, 0.0, 0.0
            ratio, levels, empties = 1.0, [], 0
            for ms in range(120000):
                produced_in += RATE * (1 + ppm * 1e-6) / 1000 / 128
                while produced_in >= 1:
                    produced_in -= 1
                    queue_level += 1
                sink_frames = max(0.0, sink_frames - RATE / 1000)
                while sink_frames < 1764:  # 40 ms device buffer
                    if queue_level < 1:
                        empties += ms > 30000
                        break
                    ratio = drift.update(queue_level - 1)
                    queue_level -= 1
                    sink_frames += len(resampler.process(np.zeros((128, 2)), ratio))
                levels.append(queue_level)
            settled = np.asarray(levels[30000:])
            self.assertEqual(empties, 0, ppm)
            self.assertLess(settled.max(), 12, ppm)
            self.assertAlmostEqual(ratio, 1 - ppm * 1e-6, delta=1e-4)


if __name__ == '__main__':
    unittest.main()


class RelativeHapticTests(unittest.TestCase):
    def run_levels(self, relative, levels, seconds=2.0):
        from maek_audio import MaekAudioProcessor, ProcessorConfig
        rng = np.random.default_rng(0)
        cfg = ProcessorConfig(sample_rate_hz=RATE, channel_order=('left', 'center', 'right'),
                              haptic_floor_rms=0.008, haptic_full_rms=0.03,
                              haptic_relative=relative, haptic_margin_db=4, haptic_range_db=8)
        proc = MaekAudioProcessor(cfg)
        out = []
        for amp in levels:
            peak = 0.0
            for _ in range(int(seconds * RATE / 512)):
                noise = rng.normal(0, 0.004, (512, 3))
                t = np.arange(512) / RATE
                tone = amp * np.sin(2 * np.pi * 500 * t)[:, None] * np.array([1, 0, 0])
                result = proc.process((noise + tone).astype(np.float32), sample_rate_hz=RATE)
                peak = max(peak, result.haptic_left)
            out.append(peak)
        return out

    def test_far_and_near_sounds_feel_similar_in_relative_mode(self):
        quiet, far, near = self.run_levels(1.0, [0.0, 0.012, 0.25])
        self.assertLess(quiet, 0.05)
        self.assertGreater(far, 0.5)
        self.assertGreater(far / near, 0.5)
        quiet0, far0, near0 = self.run_levels(0.0, [0.0, 0.012, 0.25])
        self.assertLess(far0 / near0, far / near)  # level mode keeps the big difference
