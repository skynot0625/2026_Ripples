"""Stateful, block-based processing of three synchronous microphone channels.

The core does not capture audio, operate motors, play audio, send data, or claim
to cancel acoustic echo. All sound pressure and motor limits require calibration
on the physical installation.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple
import math

import numpy as np


CHANNELS = ("left", "center", "right")


@dataclass(frozen=True)
class ProcessorConfig:
    sample_rate_hz: int
    channel_order: Tuple[str, str, str]
    # Coordinates are metres relative to the device: +x right, +y forward.
    # Leave None until the physical array is measured and synchronization checked.
    mic_positions_m: Optional[Dict[str, Tuple[float, float]]] = None
    channels_synchronized: bool = False
    output_gain: float = 0.5
    processed_mix: float = 0.25
    output_peak_limit: float = 0.8
    band_low_hz: float = 120.0
    band_high_hz: float = 2800.0
    fir_taps: int = 63
    haptic_floor_rms: float = 0.005
    haptic_full_rms: float = 0.08
    haptic_center_mix: float = 0.3
    haptic_side_boost: float = 1.5
    haptic_attack_s: float = 0.04
    haptic_release_s: float = 0.20
    # 0 = level mode: floor..full RMS mapped linearly (original behaviour).
    # 1 = relative mode: dB above each mic's own slowly tracked background,
    #     so a quiet far sound and a loud near sound both drive the motor.
    haptic_relative: float = 0.0
    haptic_margin_db: float = 6.0     # relative mode: start this far above background
    haptic_range_db: float = 12.0     # relative mode: full strength this much above the start
    haptic_noise_rise_db_s: float = 1.5  # how fast the tracked background may rise
    motion_haptic_multiplier: float = 0.5
    motor_haptic_multiplier: float = 0.7
    min_direction_confidence: float = 0.35
    speed_of_sound_m_s: float = 343.0

    def __post_init__(self):
        if self.sample_rate_hz < 8000:
            raise ValueError("sample_rate_hz must be at least 8000")
        if sorted(self.channel_order) != sorted(CHANNELS):
            raise ValueError("channel_order must contain left, center, right once each")
        if self.fir_taps < 15 or self.fir_taps % 2 != 1:
            raise ValueError("fir_taps must be odd and at least 15")
        if not (0 < self.band_low_hz < self.band_high_hz < self.sample_rate_hz / 2):
            raise ValueError("band frequencies must fit inside Nyquist")
        if not (0 <= self.processed_mix <= 1):
            raise ValueError("processed_mix must be in [0, 1]")
        if not (0 < self.output_peak_limit <= 1) or self.output_gain < 0:
            raise ValueError("invalid output gain or peak limit")
        if not (0 <= self.haptic_floor_rms < self.haptic_full_rms <= 1):
            raise ValueError("invalid haptic RMS mapping")
        if not (0 <= self.haptic_center_mix <= 1 and self.haptic_side_boost >= 0):
            raise ValueError("invalid haptic mixing")
        if not (0 <= self.haptic_relative <= 1 and self.haptic_margin_db >= 0
                and self.haptic_range_db > 0 and self.haptic_noise_rise_db_s > 0):
            raise ValueError("invalid relative haptic settings")
        if min(self.haptic_attack_s, self.haptic_release_s) <= 0:
            raise ValueError("haptic attack and release must be positive")
        if not (0 <= self.motion_haptic_multiplier <= 1):
            raise ValueError("motion_haptic_multiplier must be in [0, 1]")
        if not (0 <= self.motor_haptic_multiplier <= 1):
            raise ValueError("motor_haptic_multiplier must be in [0, 1]")
        if not (0 <= self.min_direction_confidence <= 1):
            raise ValueError("min_direction_confidence must be in [0, 1]")
        if self.speed_of_sound_m_s <= 0:
            raise ValueError("speed_of_sound_m_s must be positive")
        if self.mic_positions_m is not None:
            if set(self.mic_positions_m) != set(CHANNELS):
                raise ValueError("mic_positions_m must specify left, center, right")
            positions = np.asarray([self.mic_positions_m[name] for name in CHANNELS], dtype=float)
            if positions.shape != (3, 2) or not np.isfinite(positions).all():
                raise ValueError("mic_positions_m requires three finite 2-D coordinates")
            if np.linalg.matrix_rank(positions[1:] - positions[0]) < 2:
                raise ValueError("microphone coordinates must not be collinear")


@dataclass
class ProcessResult:
    # Normalized float32 samples, (frames, 2), in left/right order.
    stereo_pcm: np.ndarray
    # Controller targets only. A motor driver must map these to its own PWM range.
    haptic_left: float
    haptic_right: float
    # Relative to device front: right positive; None means not trustworthy/available.
    direction_deg: Optional[float]
    direction_confidence: float
    # Level bias is not a measured angle: -1 left, +1 right.
    level_bias: float
    rms_left: float
    rms_center: float
    rms_right: float
    input_clipped: bool
    aec_active: bool = False
    capture_start_time_ns: Optional[int] = None


def _bandpass_fir(sample_rate_hz, low_hz, high_hz, taps):
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2
    high = (2 * high_hz / sample_rate_hz) * np.sinc(2 * high_hz * n / sample_rate_hz)
    low = (2 * low_hz / sample_rate_hz) * np.sinc(2 * low_hz * n / sample_rate_hz)
    kernel = (high - low) * np.hamming(taps)
    midpoint = math.sqrt(low_hz * high_hz)
    response = np.abs(np.sum(kernel * np.exp(-2j * np.pi * midpoint * np.arange(taps) / sample_rate_hz)))
    if response > 1e-12:
        kernel /= response
    return kernel.astype(np.float32)


def _soft_limit(values, peak_limit):
    knee = 0.8 * peak_limit
    magnitude = np.abs(values)
    out = np.where(
        magnitude <= knee,
        magnitude,
        peak_limit - (peak_limit - knee) * np.exp(-np.maximum(magnitude - knee, 0) / (peak_limit - knee)),
    )
    return (np.sign(values) * out).astype(np.float32)


def _gcc_phat_lag(a, b, max_lag):
    """Return fractional lag (a relative to b) and a heuristic peak quality."""
    length = len(a)
    fft_length = 1 << (2 * length - 1).bit_length()
    cross = np.fft.rfft(a, fft_length) * np.conj(np.fft.rfft(b, fft_length))
    cross /= np.maximum(np.abs(cross), 1e-12)
    corr = np.fft.irfft(cross, fft_length)
    lags = np.arange(-max_lag, max_lag + 1)
    values = np.abs(corr[lags % fft_length])
    index = int(np.argmax(values))
    peak = float(values[index])
    background = float(np.median(values)) + 1e-12
    quality = float(np.clip((peak / background - 2.0) / 8.0, 0, 1))
    fractional = 0.0
    if 0 < index < len(values) - 1:
        left, center, right = values[index - 1:index + 2]
        denominator = left - 2 * center + right
        if abs(denominator) > 1e-12:
            fractional = float(np.clip(0.5 * (left - right) / denominator, -0.5, 0.5))
    return float(lags[index] + fractional), quality


class MaekAudioProcessor:
    """Process contiguous blocks in capture order; one instance per audio stream."""

    def __init__(self, config: ProcessorConfig):
        self.config = config
        self._column = [config.channel_order.index(name) for name in CHANNELS]
        self._kernel = _bandpass_fir(
            config.sample_rate_hz, config.band_low_hz, config.band_high_hz, config.fir_taps
        )
        self._filter_tail = np.zeros((config.fir_taps - 1, 3), dtype=np.float32)
        self._dry_delay = (config.fir_taps - 1) // 2
        self._dry_tail = np.zeros((self._dry_delay, 2), dtype=np.float32)
        self._haptic = np.zeros(2, dtype=np.float64)
        self._noise_db = None  # per-mic background level (dB) for relative haptics
        self._positions = None
        if config.mic_positions_m is not None:
            self._positions = np.asarray(
                [config.mic_positions_m[name] for name in CHANNELS], dtype=np.float64
            )

    def reset(self):
        """Call after a capture gap, stream restart, or changed input format."""
        self._filter_tail.fill(0)
        self._dry_tail.fill(0)
        self._haptic.fill(0)
        # The tracked background level survives gaps: the room did not change.

    def _direction(self, correlation_signal, rms):
        cfg = self.config
        if (self._positions is None or not cfg.channels_synchronized
                or len(correlation_signal) < max(128, cfg.fir_taps * 2)):
            return None, 0.0
        if float(max(rms)) < cfg.haptic_floor_rms * 2:
            return None, 0.0
        # Two independent time differences are enough for a 2-D plane wave;
        # the third provides a consistency check. This is not multi-source DOA.
        rows = []
        distances = []
        qualities = []
        for first, second in ((0, 1), (2, 1), (0, 2)):
            baseline = self._positions[first] - self._positions[second]
            max_lag = min(
                int(math.ceil(np.linalg.norm(baseline) * cfg.sample_rate_hz / cfg.speed_of_sound_m_s)) + 1,
                len(correlation_signal) // 4,
            )
            lag, quality = _gcc_phat_lag(
                correlation_signal[:, first], correlation_signal[:, second], max_lag
            )
            rows.append(baseline)
            distances.append(-cfg.speed_of_sound_m_s * lag / cfg.sample_rate_hz)
            qualities.append(quality)
        matrix = np.asarray(rows)
        distance = np.asarray(distances)
        direction, _, _, _ = np.linalg.lstsq(matrix, distance, rcond=None)
        length = float(np.linalg.norm(direction))
        if length < 1e-6:
            return None, 0.0
        residual = float(np.linalg.norm(matrix @ direction - distance))
        scale = max(float(np.linalg.norm(distance)), 0.01)
        fit = float(np.clip(1 - 3 * residual / scale, 0, 1))
        physical = float(np.clip(1 - abs(length - 1), 0, 1))
        activity = float(np.clip((max(rms) - cfg.haptic_floor_rms) / 0.02, 0, 1))
        confidence = float(min(qualities) * fit * physical * activity)
        if confidence < cfg.min_direction_confidence:
            return None, confidence
        normalized = direction / length
        return float(math.degrees(math.atan2(normalized[0], normalized[1]))), confidence

    def process(
        self,
        pcm_block,
        *,
        sample_rate_hz,
        capture_start_time_ns=None,
        is_moving=False,
        motor_active=False,
    ):
        """Return speaker samples and motor targets for one contiguous PCM block.

        Input is a (frames, 3) float array normalized to [-1, 1]. Supply the
        actual channel order in ProcessorConfig. Integer PCM needs an explicit
        capture adapter because the real I2S word alignment is not known yet.
        """
        cfg = self.config
        if sample_rate_hz != cfg.sample_rate_hz:
            raise ValueError("sample rate changed; create a new processor")
        array = np.asarray(pcm_block)
        if array.ndim != 2 or array.shape[1] != 3 or array.shape[0] == 0:
            raise ValueError("pcm_block must have shape (nonzero frames, 3)")
        if array.dtype.kind != "f":
            raise TypeError("pcm_block must be normalized floating-point PCM")
        if not np.isfinite(array).all():
            raise ValueError("pcm_block contains NaN or infinity")
        input_clipped = bool(np.any(np.abs(array) >= 1))
        ordered = np.clip(array[:, self._column], -1, 1).astype(np.float32, copy=False)
        extended = np.concatenate((self._filter_tail, ordered), axis=0)
        filtered = np.stack(
            [np.convolve(extended[:, channel], self._kernel, mode="valid") for channel in range(3)],
            axis=1,
        ).astype(np.float32)
        self._filter_tail = extended[-(cfg.fir_taps - 1):].copy()
        rms = np.sqrt(np.mean(np.square(filtered.astype(np.float64)), axis=0))

        left_raw = ordered[:, 0]
        right_raw = ordered[:, 2]
        dry = np.stack((left_raw, right_raw), axis=1)
        if cfg.processed_mix:
            if self._dry_delay:
                with_tail = np.concatenate((self._dry_tail, dry), axis=0)
                dry_aligned = with_tail[:len(dry)]
                self._dry_tail = with_tail[-self._dry_delay:].copy()
            else:
                dry_aligned = dry
            wet = np.stack((filtered[:, 0], filtered[:, 2]), axis=1)
            stereo = ((1 - cfg.processed_mix) * dry_aligned + cfg.processed_mix * wet) * cfg.output_gain
        else:
            stereo = dry * cfg.output_gain
        stereo = _soft_limit(stereo, cfg.output_peak_limit)

        activation = np.clip(
            (rms - cfg.haptic_floor_rms) / (cfg.haptic_full_rms - cfg.haptic_floor_rms), 0, 1
        )
        if cfg.haptic_relative > 0:
            level_db = 20 * np.log10(np.maximum(rms, 1e-7))
            step = len(ordered) / cfg.sample_rate_hz
            if self._noise_db is None:
                self._noise_db = level_db.copy()
            else:
                # Background follows quiet moments quickly, rises only slowly,
                # so sustained sound is not mistaken for background too soon.
                falling = level_db < self._noise_db
                fall = 1 - math.exp(-step / 0.5)
                self._noise_db = np.where(
                    falling,
                    self._noise_db + fall * (level_db - self._noise_db),
                    np.minimum(level_db, self._noise_db + cfg.haptic_noise_rise_db_s * step),
                )
            above = level_db - self._noise_db - cfg.haptic_margin_db
            relative = np.clip(above / cfg.haptic_range_db, 0, 1)
            # Never react to a signal below the absolute floor (true silence / dead mic).
            relative = np.where(rms >= cfg.haptic_floor_rms * 0.25, relative, 0.0)
            activation = (1 - cfg.haptic_relative) * activation + cfg.haptic_relative * relative
        side_mean = (activation[0] + activation[2]) / 2
        side_difference = cfg.haptic_side_boost * (activation[0] - activation[2]) / 2
        center = cfg.haptic_center_mix * activation[1]
        target = np.clip(
            np.asarray((side_mean + side_difference + center, side_mean - side_difference + center)),
            0, 1,
        )
        if is_moving:
            target *= cfg.motion_haptic_multiplier
        if motor_active:
            target *= cfg.motor_haptic_multiplier
        duration = len(ordered) / cfg.sample_rate_hz
        for index in range(2):
            time_constant = cfg.haptic_attack_s if target[index] > self._haptic[index] else cfg.haptic_release_s
            factor = math.exp(-duration / time_constant)
            self._haptic[index] = factor * self._haptic[index] + (1 - factor) * target[index]

        level_bias = float((rms[2] - rms[0]) / max(rms[0] + rms[2], 1e-12))
        # PHAT on the raw capture block preserves arrival-time peaks better
        # than on the narrow band used for haptic level detection.
        direction, confidence = self._direction(ordered, rms)
        return ProcessResult(
            stereo_pcm=stereo,
            haptic_left=float(self._haptic[0]),
            haptic_right=float(self._haptic[1]),
            direction_deg=direction,
            direction_confidence=confidence,
            level_bias=level_bias,
            rms_left=float(rms[0]),
            rms_center=float(rms[1]),
            rms_right=float(rms[2]),
            input_clipped=input_clipped,
            capture_start_time_ns=capture_start_time_ns,
        )
