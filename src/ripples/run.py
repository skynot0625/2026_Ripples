"""Ripples recording replay + two-state LED display. USB is opt-in and confirmed."""
import argparse
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import signal
import time

import numpy as np

from ripples.hardware.rgb565 import InstallationEncoder
from ripples.hardware.transport import LEDUSB, VALID_MASK
from ripples.sensors import load_recordings
from ripples.memory import SpatialMemory
from ripples.visualization import RipplesVisual, SLEEP_MEMORY_GAIN, DREAM_TAU_S
from ripples.spikes import send_frames, spike_frame

from ripples.paths import ROOT, DATA_DIR, RUNTIME_DIR


def import_events(memory, events):
    groups = defaultdict(list)
    for event in events:
        groups[event["recording_key"], event["sequence"]].append(event)
    count = 0
    # Audio batches must be ordered by session/start_frame before advancing the cursor.
    for (key, sequence), batch in sorted(groups.items()):
        end = sequence+batch[0]["frames"] if "frames" in batch[0] else -1
        count += memory.import_recording(key, batch, sequence=sequence, end_sequence=end)
    return count


def recorded_spikes():
    with np.load((DATA_DIR / "spikes" / "example_spikes.npz"), allow_pickle=False) as data:
        ids, steps = data["neuron_ids"], data["steps"]
        n = int(data["n_steps"])
        if int(data["n_neurons"]) != 8460 or float(data["gain"]) != 9.5:
            raise ValueError("unexpected reservoir recording")
    while True:
        begin = 0
        for tick in range(0, n, 40):
            end = steps.searchsorted(tick+40)
            yield spike_frame(ids[begin:end]), end-begin
            begin = end


def live_spikes():
    from ripples.reservoir import BiologicalEIReservoir, led_config, model_frames, torch
    class WithCount:
        def render(self, frame, spike_count):
            return frame, spike_count
    torch.set_num_threads(1)
    cfg = led_config(0, 9.5)
    model = BiologicalEIReservoir(cfg, blockwise_global=True)
    yield from model_frames(model, 40, 0, visual=WithCount())


class Preview:
    def __init__(self, xyz, *, audio_status="absent", criticality="activity", live=False):
        from PIL import Image, ImageDraw
        self.Image, self.Draw = Image, ImageDraw
        xyz = xyz[VALID_MASK]
        xyz = (xyz-xyz.min(axis=0))/(xyz.max(axis=0)-xyz.min(axis=0))
        self.points = np.column_stack((45+xyz[:, 0]*340+xyz[:, 1]*65,
                                        490-xyz[:, 2]*350-xyz[:, 1]*35)).astype(int)
        self.order = np.argsort(xyz[:, 1])
        self.frames = []
        self.audio_status = audio_status
        self.criticality = criticality
        self.live = live

    def add(self, active, sleep, seconds):
        im = self.Image.new("RGB", (1020, 570), (7, 11, 20))
        draw = self.Draw.Draw(im)
        title = "RIPPLES LIVE | RSSI position estimate; check mode in live-status.json" if self.live else "RIPPLES | real Hyeol recording + synthetic circular RSSI"
        draw.text((24, 15), title, fill=(220, 230, 245))
        draw.text((24, 36), "Height = sensor distance/features, not reconstructed world height. Digital preview exposure.", fill=(145, 160, 180))
        for offset, label, frame in ((0, "ACTIVATE | transient observation traces", active),
                                      (510, f"SLEEP | mean + std + {self.criticality}", sleep)):
            draw.text((offset+24, 70), label, fill=(210, 220, 240))
            colors = frame[VALID_MASK]
            for i in self.order:
                x, y = self.points[i]
                draw.point((int(x+offset), int(y)), fill=(17, 23, 32))
            # Draw lit points last so unlit rear layers cannot hide a nearby source.
            for i in self.order[np.max(colors[self.order], axis=1) > 5]:
                x, y = self.points[i]
                color = tuple((255*(colors[i]/255.)**.7).astype(int))
                draw.ellipse((int(x+offset-1), int(y-1), int(x+offset+1), int(y+1)), fill=color)
        draw.text((24, 526), f"{seconds:05.1f}s | Hyeol: depth + ONE upward point | Maek audio: {self.audio_status}", fill=(140, 160, 185))
        self.frames.append(im.convert("P", palette=self.Image.Palette.ADAPTIVE, colors=128))

    def save(self, path, fps):
        if not self.frames:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.frames[0].save(path, save_all=True, append_images=self.frames[1:],
                            duration=round(2000/fps), loop=0, optimize=False)
        self.frames[-1].convert("RGB").save(path.with_suffix(".png"))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, default=DATA_DIR/"sensors")
    p.add_argument("--db", type=Path, default=RUNTIME_DIR/"demo.sqlite3")
    p.add_argument("--state", choices=("activate", "sleep"))
    p.add_argument("--state-file", type=Path, default=RUNTIME_DIR/"state.txt")
    p.add_argument("--fps", type=float, default=10.)
    p.add_argument("--seconds", type=float, default=60., help="0 = continuous USB")
    p.add_argument("--port", help="requires interactive board confirmation")
    p.add_argument("--reservoir", choices=("live", "recorded"), default="live")
    p.add_argument("--transition-direction", choices=("up", "down", "up_flat"), default="up", help="state-change sweep direction; RGB output budgets always enabled")
    p.add_argument("--criticality", choices=("activity", "slow_waves", "legacy_waves"), default="activity")
    p.add_argument("--dream-strength", type=float, default=.45)
    p.add_argument("--preview", type=Path)
    p.add_argument("--watch", action="store_true", help="import new/updated recording files every 10 seconds")
    args = p.parse_args()
    if not np.isfinite(args.fps) or not 0 < args.fps <= 10 or not np.isfinite(args.seconds) or args.seconds < 0:
        p.error("fps must be in (0,10], seconds must be nonnegative")
    if args.seconds == 0 and (not args.port or args.preview):
        p.error("continuous mode requires USB and cannot collect a GIF")
    if args.preview and args.seconds > 120:
        p.error("GIF preview is limited to 120 seconds to bound RAM")
    if not np.isfinite(args.dream_strength) or not 0 <= args.dream_strength <= 1:
        p.error("dream-strength must be in [0,1]")
    events, dataset, info = load_recordings(args.data_dir)
    memory = SpatialMemory(args.db, namespace="demo")
    try:
        info["new_recording_batches"] = import_events(memory, events)
        info["storage"] = memory.report()
        saved_memory = memory.snapshot()
    finally:
        memory.close()
    print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    if args.state or not args.state_file.exists():
        args.state_file.write_text(args.state or "activate", encoding="utf-8")
    visual = RipplesVisual(criticality=args.criticality, dream_strength=args.dream_strength, transition_direction=args.transition_direction)
    visual.set_memory(saved_memory)
    other = RipplesVisual("sleep", criticality=args.criticality, dream_strength=args.dream_strength) if args.preview else None
    active_preview = RipplesVisual("activate") if args.preview else None
    if other:
        other.set_memory(saved_memory)
    audio_status = "SYNTHETIC" if any(e.get("audio_demo") for e in events) else ("recorded" if info["wav_files"] else "absent")
    preview = Preview(visual.waves.xyz, audio_status=audio_status, criticality=args.criticality) if args.preview else None
    stop = RUNTIME_DIR/"stop"
    stop.unlink(missing_ok=True)  # this invocation is an explicit request to start
    interrupted = False

    def halt(*_):
        nonlocal interrupted
        interrupted = True
    previous_signal = signal.signal(signal.SIGINT, halt)
    source = live_spikes() if args.reservoir == "live" else recorded_spikes()
    timings = deque(maxlen=10000)
    encoder = InstallationEncoder()
    info.update(reservoir=args.reservoir, neurons=8460, gain=9.5, dt_ms=.1,
                steps_per_frame=40, requested_fps=args.fps, usb_verified=False,
                criticality_display=args.criticality, dream_strength=args.dream_strength,
                sleep_style="dream-v2", sleep_memory_gain=SLEEP_MEMORY_GAIN, dream_tau_s=DREAM_TAU_S,
                transition_direction=args.transition_direction, output_limits={"channel_max": 96, "physical_weighted_rgb_mean": 18},
                replay_does_not_add_observations=True)
    pool = ThreadPoolExecutor(max_workers=1) if args.watch else None
    pending = None
    last_check = -10.
    signature = None

    def folder_signature():
        return tuple(sorted((str(f), f.stat().st_size, f.stat().st_mtime_ns)
                            for f in args.data_dir.rglob("*")
                            if f.is_file() and f.suffix.lower() in (".zip", ".bin", ".wav", ".json")))

    if pool:
        signature = folder_signature()

    def refresh():
        loaded_signature = folder_signature()
        new_events, _, new_info = load_recordings(args.data_dir)
        db = SpatialMemory(args.db, namespace="demo")
        try:
            imported = import_events(db, new_events)
            return new_events, new_info, db.snapshot(), imported, loaded_signature
        finally:
            db.close()

    def frames():
        nonlocal pending, last_check, signature, events
        index, cycle = 0, -1
        start = last = None
        for i, (raw, count) in enumerate(source):
            # Exclude model construction/warmup from the sensor playback clock.
            now = time.monotonic()
            if start is None:
                start = last = now
            elapsed = now-start if args.port else i/args.fps
            if interrupted or stop.exists() or (args.seconds and elapsed >= args.seconds):
                break
            if pool and pending and pending.done():
                try:
                    new_events, new_info, snapshot, imported, signature = pending.result()
                    events = new_events
                    info.update(new_info)
                    visual.set_memory(snapshot)
                    if other:
                        other.set_memory(snapshot)
                    # Advance to the current playback point without replaying old traces.
                    cycle = int(elapsed/info["duration_s"])
                    index = int(np.searchsorted([e["t"] for e in events], elapsed % info["duration_s"], side="right"))
                    print(f"Imported {imported} new batches; spatial memory refreshed", flush=True)
                except Exception as exc:
                    print(f"Input refresh postponed: {exc}", flush=True)
                pending = None
            if pool and pending is None and elapsed-last_check >= 10.:
                last_check = elapsed
                if folder_signature() != signature:
                    pending = pool.submit(refresh)
            dt = max(.001, now-last) if args.port and i else 1/args.fps
            last = now
            current_cycle = int(elapsed/info["duration_s"])
            if current_cycle != cycle:
                cycle, index = current_cycle, 0
            local_t = elapsed % info["duration_s"]
            batch = []
            while index < len(events) and events[index]["t"] <= local_t:
                batch.append(events[index])
                index += 1
            # Malformed/intermediate manual writes leave the previous state intact.
            try:
                state = args.state_file.read_text(encoding="utf-8-sig").strip()
                visual.set_state(state)
            except (OSError, ValueError):
                pass
            begin = time.perf_counter()
            frame = visual.render(raw, count, events=batch, dt=dt)
            encoder.encode_packet(frame, i)
            timings.append(time.perf_counter()-begin)
            if preview:
                a = active_preview.render(raw, count, events=batch, dt=dt)
                b = other.render(raw, count, events=batch, dt=dt)
                if i % 2 == 0:
                    preview.add(a, b, elapsed)
            yield frame

    try:
        if args.port:
            from ripples.hardware.board import confirm_board
            confirm_board(args.port, "rgb565")
            with LEDUSB(args.port, version="rgb565") as link:
                info["frames"] = send_frames(link, frames(), args.fps)
            info["usb_verified"] = True
        else:
            info["frames"] = sum(1 for _ in frames())
    finally:
        signal.signal(signal.SIGINT, previous_signal)
        source.close()
        if pool:
            pool.shutdown(wait=True, cancel_futures=True)
    if preview:
        preview.save(args.preview, args.fps)
    db = SpatialMemory(args.db, namespace="demo")
    try:
        info["storage"] = db.report()
    finally:
        db.close()
    info["render_encode_ms"] = dict(zip(("median", "p95", "max"),
        np.percentile(timings, [50, 95, 100])*1000)) if timings else {}
    report = args.db.with_name("last-run.json")
    report.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"frames": info["frames"], "render_encode_ms": info["render_encode_ms"],
                      "report": str(report)}, indent=2), flush=True)


if __name__ == "__main__":
    main()
