"""Short, quiet identification tones; one logical speaker at a time."""
import threading
import time

import numpy as np


TEST_PEAK = 0.02  # -34 dBFS peak; independent of the exhibition master setting.


def identification_tone(number, rate):
    """One to four soft 660 Hz pings identify OUT 1 to OUT 4 by ear."""
    if number not in (1, 2, 3, 4):
        raise ValueError("speaker number must be 1 to 4")
    pulse_frames = round(.24 * rate)
    t = np.arange(pulse_frames) / rate
    envelope = np.sin(np.linspace(0, np.pi, pulse_frames)) ** 2
    pulse = (TEST_PEAK * envelope * np.sin(2 * np.pi * 660 * t)).astype(np.float32)
    samples = np.zeros(round((number * .48 + .30) * rate), np.float32)
    for i in range(number):
        start = round(i * .48 * rate)
        samples[start:start + pulse_frames] = pulse
    return samples


def play_speaker_test(sd, device, rate, channels, mapping, labels, selected=None):
    numbers = [selected] if selected is not None else [1, 2, 3, 4]
    print("Speaker test: 2% digital peak (-34 dBFS). Set both amplifier gains LOW.", flush=True)
    print("Only one speaker should sound. Ctrl+C stops. No automatic repeat.", flush=True)
    try:
        for number in numbers:
            target = mapping[number - 1]
            label = labels[number - 1]
            print(f"OUT {number}: {label}; channel {target}; {number} soft ping(s)", flush=True)
            samples = identification_tone(number, rate)
            cursor = 0
            statuses = []
            finished = threading.Event()

            def callback(outdata, frames, timing, status):
                nonlocal cursor
                outdata.fill(0)
                if status:
                    statuses.append(str(status))
                    raise sd.CallbackAbort
                count = min(frames, len(samples) - cursor)
                outdata[:count, target] = samples[cursor:cursor + count]
                cursor += count
                if cursor >= len(samples):
                    raise sd.CallbackStop

            with sd.OutputStream(device=device, samplerate=rate, channels=channels,
                                 dtype="float32", blocksize=1024, latency="high",
                                 callback=callback, finished_callback=finished.set) as stream:
                # A finite timeout also handles a stalled or unplugged driver.
                deadline = time.monotonic() + len(samples) / rate + 3
                while not finished.wait(.05):
                    if time.monotonic() >= deadline:
                        stream.abort()
                        raise RuntimeError("Speaker test timed out; output stopped")
            if statuses or cursor != len(samples):
                raise RuntimeError(f"Speaker test interrupted by device: {statuses}")
            if number != numbers[-1]:
                print("Pause: 2 seconds", flush=True)
                sd.sleep(2000)
    except KeyboardInterrupt:
        print("Speaker test stopped.", flush=True)
        return
    print("Test ended. Confirm by ear that only the indicated speaker sounded each time.", flush=True)
