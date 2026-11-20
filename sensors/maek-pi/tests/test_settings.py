import os
import tempfile
import unittest
import wave
import io

import numpy as np

from maek_audio import SettingsError, load_settings
from maek_audio.settings import decode_pcm, encode_pcm, settings_from_text

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "config", "maek_audio.ini")


def filled(**replace):
    with open(TEMPLATE, encoding="utf-8") as handle:
        text = handle.read()
    values = {"sample_rate_hz =\n": "sample_rate_hz = 48000\n", "sample_format =\n": "sample_format = s16le\n"}
    values.update(replace)
    for old, new in values.items():
        text = text.replace(old, new, 1)
    return text


class SettingsTests(unittest.TestCase):
    def test_blank_template_lists_required_fields(self):
        with self.assertRaises(SettingsError) as caught:
            load_settings(TEMPLATE)
        message = str(caught.exception)
        self.assertIn("sample_rate_hz is required", message)
        self.assertIn("sample_format is required", message)

    def test_filled_template_loads_with_defaults(self):
        settings = settings_from_text(filled())
        self.assertEqual(settings.processor.sample_rate_hz, 48000)
        self.assertEqual(settings.input.frames_per_block, 480)
        self.assertEqual(settings.speaker.sample_format, "s16le")
        self.assertEqual(settings.processor.output_gain, 0.5)
        self.assertIsNone(settings.processor.mic_positions_m)

    def test_file_path_and_env(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "maek_audio.ini")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(filled())
            os.environ["MAEK_AUDIO_CONFIG"] = path
            try:
                self.assertEqual(load_settings().source_path, path)
            finally:
                del os.environ["MAEK_AUDIO_CONFIG"]

    def test_positions_tuning_and_channel_order(self):
        text = filled(**{
            "channel_order = left, center, right": "channel_order = right, left, center",
            "left =\n": "left = -0.10, 0.00\n",
            "center =\n": "center = 0.00, 0.12\n",
            "right =\n": "right = 0.10, 0.00\n",
            "output_gain =\n": "output_gain = 0.7  # louder\n",
        })
        settings = settings_from_text(text)
        self.assertEqual(settings.processor.channel_order, ("right", "left", "center"))
        self.assertEqual(settings.processor.mic_positions_m["center"], (0.0, 0.12))
        self.assertEqual(settings.processor.output_gain, 0.7)

    def test_partial_positions_and_rate_mismatch_rejected(self):
        text = filled(**{"left =\n": "left = -0.1, 0\n"})
        with self.assertRaises(SettingsError):
            settings_from_text(text)
        text = filled()
        head, tail = text.split("[speaker]", 1)
        text = head + "[speaker]" + tail.replace("sample_rate_hz =\n", "sample_rate_hz = 44100\n", 1)
        with self.assertRaises(SettingsError):
            settings_from_text(text)

    def test_syntax_error_is_settings_error(self):
        with self.assertRaises(SettingsError):
            settings_from_text("[input]\nsample_rate_hz = 1\nsample_rate_hz = 2\n")

    def test_decode_formats(self):
        values = np.array([[0.5, -0.25, 0.0]] * 4)
        for fmt in ("s16le", "s24le", "s32le", "float32le"):
            decoded = decode_pcm(encode_pcm(values, fmt), fmt, channels=3)
            np.testing.assert_allclose(decoded, values, atol=1e-4)
        # ALSA S24_LE: 24-bit value in the low bits of a 32-bit word.
        ints = np.array([1 << 22, -(1 << 21), 0], dtype=np.int32) & 0xFFFFFF
        decoded = decode_pcm(ints.astype("<u4").tobytes(), "s24_32le", channels=3)
        np.testing.assert_allclose(decoded[0], [0.5, -0.25, 0.0])

    def test_s24_32_source_wav_is_repacked(self):
        settings = settings_from_text(filled(**{"sample_format =\n": "sample_format = s24_32le\n"}))
        ints = (np.full((48000, 3), 1 << 20, dtype=np.int32) & 0xFFFFFF).astype("<u4")
        chunker = settings.make_chunker()
        chunks = chunker.append_bytes(settings.wav_source_bytes(ints.tobytes()))
        self.assertEqual(len(chunks), 1)
        with wave.open(io.BytesIO(chunks[0].wav_bytes)) as reader:
            self.assertEqual(reader.getsampwidth(), 3)
            first = reader.readframes(1)
        self.assertEqual(first[:3], (1 << 20).to_bytes(3, "little"))

    def test_motor_mapping(self):
        settings = settings_from_text(filled().replace("deadzone = 0.0", "deadzone = 0.1"))

        class Result:
            haptic_left = 0.05
            haptic_right = 0.5

        self.assertEqual(settings.motor_commands(Result()), (0, 128))

    def test_end_to_end_speaker_bytes(self):
        settings = settings_from_text(filled())
        processor = settings.make_processor()
        raw = encode_pcm(np.zeros((480, 3)), "s16le")
        result = processor.process(settings.decode_input(raw), sample_rate_hz=48000)
        self.assertEqual(len(settings.encode_speaker(result.stereo_pcm)), 480 * 2 * 2)


if __name__ == "__main__":
    unittest.main()
