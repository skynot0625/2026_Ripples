"""Map reservoir spike IDs to the partner installation and play ordered frames."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import math
import time

import numpy as np
from ripples.hardware.rgb565 import InstallationEncoder

from ripples.hardware.transport import LEDUSB, PinMaskEncoder, RGB565Encoder, VALID_MASK


# IDs follow zone, connector and then depth, skipping all absent addresses.
PIXELS = np.flatnonzero(VALID_MASK)
N_NEURONS = PIXELS.size


def spike_frame(neuron_ids):
    """Render one simulation window. Repeated spikes on a pixel keep it lit."""
    ids = np.asarray(neuron_ids)
    if ids.ndim != 1 or (ids.size and ids.dtype.kind not in 'iu'):
        raise ValueError('neuron_ids must be a one-dimensional integer array')
    if ids.size and (ids.min() < 0 or ids.max() >= N_NEURONS):
        raise ValueError(f'neuron IDs must lie in 0..{N_NEURONS - 1}')
    frame = np.zeros((*VALID_MASK.shape, 3), dtype=np.uint8)
    if ids.size:
        frame.reshape(-1, 3)[PIXELS[ids]] = (0, 160, 255)
    return frame


def recorded_frames(path, steps_per_frame):
    """Read [start, stop) windows, including silence and a partial last window."""
    with np.load(path, allow_pickle=False) as data:
        steps, ids = data['steps'], data['neuron_ids']
        length, count, dt = (data[k].item() for k in ('n_steps', 'n_neurons', 'dt_ms'))
    if not isinstance(length, int) or length <= 0 or count != N_NEURONS:
        raise ValueError(f'expected positive n_steps and n_neurons={N_NEURONS}')
    if not math.isfinite(dt) or dt <= 0:
        raise ValueError('dt_ms must be finite and positive')
    if steps.ndim != 1 or steps.shape != ids.shape or steps.dtype.kind not in 'iu':
        raise ValueError('steps and neuron_ids must be aligned one-dimensional arrays')
    if ids.dtype.kind not in 'iu' or (ids.size and (ids.min() < 0 or ids.max() >= count)):
        raise ValueError('invalid spike IDs in recording')
    if steps.size and (steps[0] < 0 or steps[-1] >= length or np.any(steps[1:] < steps[:-1])):
        raise ValueError('steps must be sorted integers in [0, n_steps)')
    print(f'{steps_per_frame * dt:g} ms of model time per frame at dt={dt:g} ms')
    start = 0
    for first_step in range(0, length, steps_per_frame):
        stop = steps.searchsorted(first_step + steps_per_frame)
        yield spike_frame(ids[start:stop])
        start = stop


def buffered_frames(frames):
    """Compute one upcoming frame while the current frame is on USB."""
    it = iter(frames)
    end = object()
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(next, it, end)
        while True:
            frame = pending.result()
            if frame is end:
                return
            pending = worker.submit(next, it, end)
            yield frame


def send_frames(link, frames, fps=10):
    """Pace submissions and keep at most two outstanding, retaining every window."""
    if not 0 < fps <= 10:
        raise ValueError('fps must be in (0, 10]')
    due = time.monotonic()
    last_report = None
    pending = 0
    completed = 0
    try:
        for frame in buffered_frames(frames):
            if pending == 2:
                ack = link.wait_next()
                pending -= 1
                completed += 1
                if ack and completed % 32 == 1:
                    now = ack["completed_at"] if ack else time.monotonic()
                    if last_report is not None:
                        seconds = ((ack["end_us"] - last_board) & 0xffffffff) / 1e6
                        print(f"{completed} displayed, {32 / (now-last_report):.2f} ACK FPS, "
                              f"{32/seconds:.2f} board FPS, "
                              f"RX={ack['rx_us']/1000:.1f} scan={ack['scan_us']/1000:.1f} ms", flush=True)
                    last_report = now
                    last_board = ack["end_us"]
            time.sleep(max(0, due - time.monotonic()))
            due = time.monotonic() + 1 / fps
            link.submit(frame)
            pending += 1
    finally:
        if link.serial.is_open:
            # Use actual transport state even if interrupted after submit().
            while link.inflight:
                link.wait_next()
                completed += 1
            link.send(spike_frame([]))
    return completed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recording', help='NPZ with steps, neuron_ids, n_neurons, n_steps, dt_ms')
    parser.add_argument('--steps-per-frame', type=int, required=True)
    parser.add_argument('--fps', type=float, default=10)
    parser.add_argument('--firmware', choices=['rgb565', 'pinmask'], required=True)
    parser.add_argument('--port', help='USB serial port. Omit for an offline encoding check.')
    args = parser.parse_args()
    if args.steps_per_frame < 1 or not 0 < args.fps <= 10:
        parser.error('steps-per-frame must be positive and fps must be in (0, 10]')
    frames = recorded_frames(args.recording, args.steps_per_frame)
    if args.port:
        from ripples.hardware.board import confirm_board
        confirm_board(args.port, args.firmware)
        with LEDUSB(args.port, version=args.firmware) as link:
            start = time.monotonic()
            count = send_frames(link, frames, args.fps)
            print(f'{count} acknowledged data frames in {time.monotonic()-start:.2f} s (includes blackout)')
    else:
        encoder = PinMaskEncoder() if args.firmware == 'pinmask' else InstallationEncoder()
        low, high, count = N_NEURONS, 0, 0
        for frame in frames:
            encoder.encode_packet(frame, count % 2**32)
            lit = int(np.count_nonzero(frame[..., 2]))
            low, high = min(low, lit), max(high, lit)
            count += 1
        print(f'{count} frames encoded, {low}..{high} lit LEDs per frame')


if __name__ == '__main__':
    main()
