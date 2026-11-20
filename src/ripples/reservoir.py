"""Stream the corrected 0.1-ms B0 reservoir to the partner LED controller."""
import argparse
import signal
import time
from itertools import takewhile
from pathlib import Path
from threading import Event

import numpy as np
from ripples.hardware.rgb565 import InstallationEncoder
import torch

from snnrc.presets import nprc_historical_dt01
from snnrc.reservoir.biological_ei import BiologicalEIReservoir
from ripples.hardware.transport import LEDUSB, PinMaskEncoder, RGB565Encoder
from ripples.spikes import N_NEURONS, send_frames, spike_frame


def led_config(seed, gain):
    cfg = nprc_historical_dt01("B0", g_global=gain, master_seed=seed)
    # Preserve expected afferents per neuron as the 800E:200I network grows.
    cfg.connectivity.p_scale *= (cfg.N - 1) / (N_NEURONS - 1)
    cfg.N_E = 4 * N_NEURONS // 5
    cfg.N_I = N_NEURONS - cfg.N_E
    cfg.name = "led_b0_dt01"
    return cfg.validate()


def model_frames(res, steps_per_frame, frame_count, record=None, visual=None):
    state = res.reset_state()
    for _ in range(round(1000 / res.cfg.dt_ms)):
        res.step(state)
    frame = 0
    while frame_count == 0 or frame < frame_count:
        fired = []
        for step in range(steps_per_frame):
            ids = np.flatnonzero(res.step(state)[0].numpy())
            fired.append(ids)
            if record is not None and ids.size:
                record.append((frame * steps_per_frame + step, ids))
        ids = np.concatenate(fired)
        raw = spike_frame(ids)
        yield raw if visual is None else visual.render(raw, spike_count=ids.size)
        frame += 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", help="omit to simulate and encode offline")
    p.add_argument("--firmware", choices=("pinmask", "rgb565"), required=True)
    p.add_argument("--visual", choices=("waves", "raw"), default="waves")
    p.add_argument("--fps", type=float, default=10)
    p.add_argument("--steps-per-frame", type=int, default=40)
    p.add_argument("--frames", type=int, help="default: continuous USB or 300 offline")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--gain", type=float, default=9.5)
    p.add_argument("--record", type=Path, help="save raw model ticks and neuron IDs to NPZ (finite runs)")
    args = p.parse_args()
    n = args.frames if args.frames is not None else (0 if args.port else 300)
    if n < 0 or args.steps_per_frame < 1 or not 0 < args.fps <= 10:
        p.error("expected nonnegative frames, positive steps-per-frame and fps in (0,10]")
    if n == 0 and (not args.port or args.record):
        p.error("offline/recorded runs require a finite positive frame count")
    if not np.isfinite(args.gain) or args.gain <= 0:
        p.error("gain must be finite and positive")

    if args.port:
        from ripples.hardware.board import confirm_board
        confirm_board(args.port, args.firmware)
    torch.set_num_threads(1)
    cfg = led_config(args.seed, args.gain)
    print(f"Building {cfg.N_E} E + {cfg.N_I} I, dt={cfg.dt_ms} ms, gain={args.gain:g}", flush=True)
    res = BiologicalEIReservoir(cfg, blockwise_global=True)
    events = [] if args.record else None
    visual = None
    if args.visual == "waves":
        from ripples.waves import SpikeWaves
        visual = SpikeWaves()
    frames = model_frames(res, args.steps_per_frame, n, events, visual=visual)
    start = time.monotonic()
    if args.port:
        with LEDUSB(args.port, version=args.firmware) as link:
            stop = Event()
            previous = signal.signal(signal.SIGINT, lambda *_: stop.set())
            try:
                count = send_frames(link, takewhile(lambda _: not stop.is_set(), frames), args.fps)
            finally:
                signal.signal(signal.SIGINT, previous)
        print(f"{count} data frames acknowledged and blackout confirmed")
    else:
        encoder = PinMaskEncoder() if args.firmware == "pinmask" else InstallationEncoder()
        lit = []
        for seq, frame in enumerate(frames):
            encoder.encode_packet(frame, seq)
            lit.append(np.count_nonzero(frame[..., 2]))
        print(f"{n} frames encoded, lit LEDs min/median/max={min(lit)}/{np.median(lit):g}/{max(lit)}")
    print(f"Elapsed {time.monotonic() - start:.2f} s including warmup")
    if events is not None and (not args.port or count == n):
        ticks = np.repeat([t for t, _ in events], [len(ids) for _, ids in events]).astype(np.int64)
        ids = np.concatenate([ids for _, ids in events]) if events else np.empty(0, np.int64)
        np.savez_compressed(args.record, steps=ticks, neuron_ids=ids, n_neurons=cfg.N,
                            n_steps=n * args.steps_per_frame, dt_ms=cfg.dt_ms, seed=args.seed,
                            gain=args.gain, steps_per_frame=args.steps_per_frame)


if __name__ == "__main__":
    main()
