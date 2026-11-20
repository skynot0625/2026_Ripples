"""Load the installer-filled INI file and convert raw PCM / motor values.

Standard library + NumPy only, so it runs on an offline Pi. Missing required
values raise SettingsError listing every problem at once.
"""

from configparser import ConfigParser, Error as ConfigParserError
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional, Tuple, Union
import os

import numpy as np

from .processor import CHANNELS, ProcessorConfig, ProcessResult
from .wav_chunks import PcmWavChunker


INPUT_FORMATS = ("s16le", "s24le", "s24_32le", "s32le", "float32le")
OUTPUT_FORMATS = ("s16le", "s24le", "s32le", "float32le")
# Bytes per sample in the raw stream.
_STREAM_WIDTH = {"s16le": 2, "s24le": 3, "s24_32le": 4, "s32le": 4, "float32le": 4}
# Bytes per sample in the 1-second source WAV (s24_32le is repacked to 3 bytes).
_WAV_WIDTH = {"s16le": 2, "s24le": 3, "s24_32le": 3, "s32le": 4}
_FULL_SCALE = {"s16le": 2.0 ** 15, "s24le": 2.0 ** 23, "s24_32le": 2.0 ** 23, "s32le": 2.0 ** 31}

_TUNING_FIELDS = (
    "output_gain", "processed_mix", "band_low_hz", "band_high_hz", "output_peak_limit",
    "haptic_floor_rms", "haptic_full_rms", "haptic_center_mix", "haptic_side_boost",
    "haptic_attack_s", "haptic_release_s", "motion_haptic_multiplier", "motor_haptic_multiplier",
    "haptic_relative", "haptic_margin_db", "haptic_range_db", "haptic_noise_rise_db_s",
)


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class InputFormat:
    sample_format: str
    frames_per_block: int

    @property
    def frame_bytes(self):
        return 3 * _STREAM_WIDTH[self.sample_format]


@dataclass(frozen=True)
class SpeakerFormat:
    sample_format: str
    sample_rate_hz: int


@dataclass(frozen=True)
class MotorMapping:
    command_min: float
    command_max: float
    deadzone: float
    update_hz: Optional[float]


@dataclass(frozen=True)
class ServerSettings:
    """Recorded for a future uploader; nothing in this package sends data."""
    upload_url: Optional[str]
    method: str
    device_id: Optional[str]


@dataclass(frozen=True)
class DeviceSettings:
    source_path: Optional[str]
    processor: ProcessorConfig
    input: InputFormat
    speaker: SpeakerFormat
    motor: MotorMapping
    server: ServerSettings

    def make_processor(self):
        from .processor import MaekAudioProcessor
        return MaekAudioProcessor(self.processor)

    def make_chunker(self, capture_start_time_ns=None):
        """1-second WAV chunker for the unprocessed source. Feed it wav_source_bytes()."""
        if self.input.sample_format not in _WAV_WIDTH:
            raise SettingsError("source WAV for %s input is not defined yet" % self.input.sample_format)
        return PcmWavChunker(
            sample_rate_hz=self.processor.sample_rate_hz,
            channel_order=self.processor.channel_order,
            sample_width_bytes=_WAV_WIDTH[self.input.sample_format],
            capture_start_time_ns=capture_start_time_ns,
        )

    def decode_input(self, raw_bytes):
        """Raw interleaved mic bytes -> float32 (frames, 3) in channel_order, -1..1."""
        return decode_pcm(raw_bytes, self.input.sample_format, channels=3)

    def wav_source_bytes(self, raw_bytes):
        """Raw mic bytes in the form PcmWavChunker.append_bytes expects (unchanged except repacking)."""
        fmt = self.input.sample_format
        _check_whole_frames(raw_bytes, fmt, 3)
        if fmt == "s24_32le":
            # Keep the low 24 bits of each little-endian 32-bit word.
            return np.frombuffer(raw_bytes, dtype=np.uint8).reshape(-1, 4)[:, :3].tobytes()
        return bytes(raw_bytes)

    def encode_speaker(self, stereo_pcm):
        """ProcessResult.stereo_pcm -> interleaved L,R bytes for the speaker device."""
        return encode_pcm(stereo_pcm, self.speaker.sample_format)

    def motor_commands(self, result: ProcessResult):
        """(left, right) driver commands from the 0-1 haptic targets."""
        return (self._motor_value(result.haptic_left), self._motor_value(result.haptic_right))

    def _motor_value(self, strength):
        m = self.motor
        strength = float(np.clip(strength, 0, 1))
        if strength < m.deadzone or strength == 0:
            return m.command_min if isinstance(m.command_min, int) else float(m.command_min)
        value = m.command_min + strength * (m.command_max - m.command_min)
        if isinstance(m.command_min, int) and isinstance(m.command_max, int):
            return int(round(value))
        return float(value)


def _check_whole_frames(raw_bytes, fmt, channels):
    frame = channels * _STREAM_WIDTH[fmt]
    if len(raw_bytes) % frame:
        raise ValueError("byte count %d is not a whole number of %d-byte frames" % (len(raw_bytes), frame))


def decode_pcm(raw_bytes, fmt, *, channels):
    _check_whole_frames(raw_bytes, fmt, channels)
    if fmt == "float32le":
        values = np.frombuffer(raw_bytes, dtype="<f4").astype(np.float32)
        return values.reshape(-1, channels)
    if fmt == "s16le":
        ints = np.frombuffer(raw_bytes, dtype="<i2").astype(np.int32)
    elif fmt == "s32le":
        ints = np.frombuffer(raw_bytes, dtype="<i4").astype(np.int64)
    elif fmt == "s24le":
        b = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = np.where(ints >= 1 << 23, ints - (1 << 24), ints)
    elif fmt == "s24_32le":
        words = np.frombuffer(raw_bytes, dtype="<u4").astype(np.int64) & 0xFFFFFF
        ints = np.where(words >= 1 << 23, words - (1 << 24), words)
    else:
        raise ValueError("unknown sample format %r" % fmt)
    return (ints / _FULL_SCALE[fmt]).astype(np.float32).reshape(-1, channels)


def encode_pcm(samples, fmt):
    array = np.clip(np.asarray(samples, dtype=np.float64), -1, 1)
    if fmt == "float32le":
        return array.astype("<f4").tobytes()
    if fmt not in OUTPUT_FORMATS:
        raise ValueError("unknown output format %r" % fmt)
    scale = _FULL_SCALE[fmt]
    ints = np.clip(np.rint(array * scale), -scale, scale - 1).astype(np.int64)
    if fmt == "s16le":
        return ints.astype("<i2").tobytes()
    if fmt == "s32le":
        return ints.astype("<i4").tobytes()
    packed = np.frombuffer(ints.astype("<i4").tobytes(), dtype=np.uint8).reshape(-1, 4)[:, :3]
    return packed.tobytes()


def _blank(value):
    return value is None or value.strip() == ""


def load_settings(path: Union[str, Path, None] = None) -> DeviceSettings:
    """Read the INI file. path -> $MAEK_AUDIO_CONFIG -> ./maek_audio.ini."""
    if path is None:
        path = os.environ.get("MAEK_AUDIO_CONFIG") or "maek_audio.ini"
    path = Path(path)
    if not path.is_file():
        raise SettingsError("settings file not found: %s" % path)
    parser = ConfigParser(inline_comment_prefixes=("#",), interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except ConfigParserError as error:
        raise SettingsError("cannot parse %s: %s" % (path, error)) from error
    return settings_from_parser(parser, source_path=str(path))


def settings_from_text(text: str) -> DeviceSettings:
    parser = ConfigParser(inline_comment_prefixes=("#",), interpolation=None)
    try:
        parser.read_string(text)
    except ConfigParserError as error:
        raise SettingsError("cannot parse settings: %s" % error) from error
    return settings_from_parser(parser, source_path=None)


def settings_from_parser(parser: ConfigParser, *, source_path=None) -> DeviceSettings:
    problems = []

    def get(section, key):
        if not parser.has_section(section) or not parser.has_option(section, key):
            return None
        value = parser.get(section, key)
        return None if _blank(value) else value.strip()

    def number(section, key, kind=float, required=False):
        raw = get(section, key)
        if raw is None:
            if required:
                problems.append("[%s] %s is required" % (section, key))
            return None
        try:
            return kind(raw)
        except ValueError:
            problems.append("[%s] %s: %r is not a %s" % (section, key, raw, kind.__name__))
            return None

    def boolean(section, key, default):
        raw = get(section, key)
        if raw is None:
            return default
        lowered = raw.lower()
        if lowered in ("true", "yes", "1", "on"):
            return True
        if lowered in ("false", "no", "0", "off"):
            return False
        problems.append("[%s] %s: %r must be true or false" % (section, key, raw))
        return default

    # [input]
    rate = number("input", "sample_rate_hz", int, required=True)
    in_fmt = get("input", "sample_format")
    if in_fmt is None:
        problems.append("[input] sample_format is required (%s)" % ", ".join(INPUT_FORMATS))
    elif in_fmt not in INPUT_FORMATS:
        problems.append("[input] sample_format %r must be one of %s" % (in_fmt, ", ".join(INPUT_FORMATS)))
    order_raw = get("input", "channel_order")
    order = None
    if order_raw is None:
        problems.append("[input] channel_order is required")
    else:
        order = tuple(part.strip() for part in order_raw.split(","))
        if sorted(order) != sorted(CHANNELS):
            problems.append("[input] channel_order %r must list left, center, right once each" % order_raw)
    block = number("input", "frames_per_block", int)
    synchronized = boolean("input", "channels_synchronized", False)

    # [mic_positions_m]
    positions = {}
    for name in CHANNELS:
        raw = get("mic_positions_m", name)
        if raw is None:
            continue
        parts = [p.strip() for p in raw.split(",")]
        try:
            if len(parts) != 2:
                raise ValueError
            positions[name] = (float(parts[0]), float(parts[1]))
        except ValueError:
            problems.append("[mic_positions_m] %s: %r must be 'x, y' in metres" % (name, raw))
    if positions and len(positions) != 3:
        problems.append("[mic_positions_m] fill all of left, center, right or leave all blank")

    # [speaker]
    spk_fmt = get("speaker", "sample_format")
    if spk_fmt is None and in_fmt in OUTPUT_FORMATS:
        spk_fmt = in_fmt
    elif spk_fmt is None and in_fmt == "s24_32le":
        spk_fmt = "s32le"
    if spk_fmt is not None and spk_fmt not in OUTPUT_FORMATS:
        problems.append("[speaker] sample_format %r must be one of %s" % (spk_fmt, ", ".join(OUTPUT_FORMATS)))
    spk_rate = number("speaker", "sample_rate_hz", int)
    if spk_rate is not None and rate is not None and spk_rate != rate:
        problems.append("[speaker] sample_rate_hz %d differs from input %d; resampling is not implemented"
                        % (spk_rate, rate))

    # [motor]
    motor_min = number("motor", "command_min", _int_or_float)
    motor_max = number("motor", "command_max", _int_or_float)
    motor_min = 0 if motor_min is None else motor_min
    motor_max = 1 if motor_max is None else motor_max
    deadzone = number("motor", "deadzone") or 0.0
    if not 0 <= deadzone < 1:
        problems.append("[motor] deadzone must be in [0, 1)")
    update_hz = number("motor", "update_hz")

    # [server]
    server = ServerSettings(
        upload_url=get("server", "upload_url"),
        method=(get("server", "method") or "POST").upper(),
        device_id=get("server", "device_id"),
    )

    # [tuning]
    tuning = {}
    for key in _TUNING_FIELDS:
        value = number("tuning", key)
        if value is not None:
            tuning[key] = value
    known = set(_TUNING_FIELDS)
    if parser.has_section("tuning"):
        for key in parser.options("tuning"):
            if key not in known:
                problems.append("[tuning] unknown key %r" % key)

    processor = None
    if not problems:
        try:
            processor = ProcessorConfig(
                sample_rate_hz=rate,
                channel_order=order,
                mic_positions_m=positions or None,
                channels_synchronized=synchronized,
                **tuning,
            )
        except ValueError as error:
            problems.append("invalid value: %s" % error)
    if problems:
        where = " in %s" % source_path if source_path else ""
        raise SettingsError("settings problems%s:\n  - %s" % (where, "\n  - ".join(problems)))

    return DeviceSettings(
        source_path=source_path,
        processor=processor,
        input=InputFormat(sample_format=in_fmt, frames_per_block=block or max(1, rate // 100)),
        speaker=SpeakerFormat(sample_format=spk_fmt, sample_rate_hz=rate),
        motor=MotorMapping(command_min=motor_min, command_max=motor_max, deadzone=deadzone, update_hz=update_hz),
        server=server,
    )


def _int_or_float(raw):
    try:
        return int(raw)
    except ValueError:
        return float(raw)


def describe(settings: DeviceSettings):
    """Human-readable summary used by `python -m maek_audio.check`."""
    p = settings.processor
    lines = [
        "input   : %d Hz, %s, order %s, %d frames/block (%.1f ms)" % (
            p.sample_rate_hz, settings.input.sample_format, ", ".join(p.channel_order),
            settings.input.frames_per_block, 1000 * settings.input.frames_per_block / p.sample_rate_hz),
        "speaker : %d Hz, %s, 2ch (left, right)" % (settings.speaker.sample_rate_hz, settings.speaker.sample_format),
        "motor   : strength 0..1 -> %s..%s, deadzone %.2f, update %s Hz" % (
            settings.motor.command_min, settings.motor.command_max, settings.motor.deadzone,
            settings.motor.update_hz if settings.motor.update_hz else "?"),
        "direction: %s" % ("enabled" if p.mic_positions_m and p.channels_synchronized
                           else "off (needs mic_positions_m and channels_synchronized = true)"),
        "server  : %s (not sent by this package)" % (settings.server.upload_url or "not set"),
        "tuning  : " + ", ".join("%s=%s" % (f.name, getattr(p, f.name)) for f in fields(p)
                                 if f.name in _TUNING_FIELDS),
    ]
    return "\n".join(lines)
