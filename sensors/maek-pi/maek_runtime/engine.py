"""Hardware-independent processing; input blocks remain in USB wire order."""
import math
import numpy as np

class Engine:
    def __init__(self, settings, gains):
        if settings.processor.sample_rate_hz != 44100 or settings.input.sample_format != 's16le':
            raise ValueError('Existing Teensy firmware requires 44100 Hz, s16le input')
        if settings.speaker.sample_rate_hz != 44100 or settings.speaker.sample_format != 's16le':
            raise ValueError('Playback adapter requires 44100 Hz, s16le output')
        if len(gains) != 3 or any(not math.isfinite(x) or not 0 <= x <= 2 for x in gains):
            raise ValueError('mic_gains requires three finite values in [0,2]')
        for name, value in vars(settings.processor).items():
            if isinstance(value, (int, float)) and not math.isfinite(value):
                raise ValueError('Non-finite processor setting: ' + name)
        if not 0 <= settings.processor.output_gain <= 8:
            raise ValueError('This runner limits output_gain to [0,8]')
        self.settings = settings
        self.processor = settings.make_processor()
        self.gains = np.asarray(gains, dtype=np.float32)
        self.previous = None
        self.generation = None
        self.reset_count = 0

    def apply(self, settings, gains):
        """Swap in new tuning / mic gains (validated like __init__); filter state restarts."""
        fresh = Engine(settings, gains)
        self.settings, self.processor, self.gains = fresh.settings, fresh.processor, fresh.gains

    def reset(self):
        self.processor.reset()
        self.previous = None
        self.generation = None
        self.reset_count += 1

    def process_many(self, packets, generation=0):
        """Process consecutive packets in one call (much cheaper per sample on a slow Pi).

        `packets` must be in order with consecutive sequence numbers; only the
        first one is checked against the previous call.
        """
        if len(packets) == 1:
            return self.process(packets[0], generation)
        first, last = packets[0], packets[-1]
        merged = type(first)(first.sequence, first.dropped, b''.join(p.pcm for p in packets))
        data, result, discontinuity = self.process(merged, generation)
        self.previous = last.sequence
        return data, result, discontinuity

    def process(self, packet, generation=0):
        discontinuity = self.generation is not None and (
            generation != self.generation or
            ((packet.sequence - self.previous) & 0xffffffff) != 1)
        if discontinuity:
            self.reset()
        self.previous, self.generation = packet.sequence, generation
        values = self.settings.decode_input(packet.pcm)
        result = self.processor.process(values * self.gains, sample_rate_hz=44100)
        return self.settings.encode_speaker(result.stereo_pcm), result, discontinuity
