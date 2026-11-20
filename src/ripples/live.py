"""Realtime center/collector -> tower preprocessing -> memory -> optional LED2 USB."""
import argparse
from collections import deque
import itertools
import json
from pathlib import Path
import signal
import time

import numpy as np

from ripples.paths import ROOT, RUNTIME_DIR
from ripples.realtime import LiveMemory, LiveSensors, validate_config, position_enabled, memory_namespace
from ripples.run import live_spikes, recorded_spikes, Preview
from ripples.visualization import RipplesVisual
from ripples.hardware.rgb565 import InstallationEncoder
from ripples.schedule import ExhibitionSchedule, StateController


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=ROOT/"configs/live.m30.json")
    p.add_argument("--center", default="http://127.0.0.1:8000")
    p.add_argument("--collector-port", type=int, help="optional raw Hyeol TCP listener, e.g. 9501")
    p.add_argument("--audio-port", type=int, help="optional raw Maek PCM TCP listener, e.g. 9502")
    p.add_argument("--bind", default="127.0.0.1", help="set tower LAN IP to receive directly from Pi")
    p.add_argument("--db", type=Path, help="default: estimated or measured DB according to position mode")
    p.add_argument("--accumulate", action="store_true", help="opt in to persistent spatial statistics; off by default")
    p.add_argument("--state", choices=("activate", "sleep"))
    p.add_argument("--state-mode", choices=("scheduled", "manual"),
                   help="default scheduled; --state implies manual unless a mode is explicit")
    p.add_argument("--schedule", type=Path, default=ROOT/"configs/schedule.m30.json")
    p.add_argument("--state-file", type=Path, default=RUNTIME_DIR/"state.txt")
    p.add_argument("--fps", type=float, default=10)
    p.add_argument("--seconds", type=float, default=0, help="wall time; default 0=continuous")
    p.add_argument("--port", help="optional LED USB port; board confirmation required")
    p.add_argument("--reservoir", choices=("live", "recorded"), default="live")
    p.add_argument("--criticality", choices=("activity", "slow_waves"), default="activity")
    p.add_argument("--preview", type=Path, help="optional finite Activate/Sleep GIF, <=120 seconds")
    p.add_argument("--report", type=Path, default=RUNTIME_DIR/"live-status.json")
    a = p.parse_args()
    if not np.isfinite(a.fps) or not 0 < a.fps <= 10 or not np.isfinite(a.seconds) or a.seconds < 0:
        p.error("fps must be (0,10]; seconds must be finite and >=0")
    if a.preview and not 0 < a.seconds <= 120:
        p.error("preview requires 0 < seconds <= 120")
    if any(port is not None and not 1024 <= port <= 65535 for port in (a.collector_port, a.audio_port)):
        p.error("collector/audio ports must be 1024..65535")
    if a.collector_port and a.collector_port == a.audio_port:
        p.error("collector and audio need separate ports")
    config = validate_config(json.loads(a.config.read_text(encoding="utf-8-sig")))
    try:
        schedule = ExhibitionSchedule(json.loads(a.schedule.read_text(encoding="utf-8-sig")))
        state_control = StateController(schedule, mode=a.state_mode or ("manual" if a.state else "scheduled"),
                                        state_file=a.state_file, initial_state=a.state)
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    # A different source/threshold is a different measurement calibration.
    config["input_modes"] = ["raw" if a.collector_port else "monitor", "pcm" if a.audio_port else "levels"]
    if a.db is None:
        name = "m30-relative.sqlite3" if memory_namespace(config) == "estimated" else "measured.sqlite3"
        if config.get("site", {}).get("led_mapping", {}).get("id") == "m30-right-room":
            name = "m30-right-room-"+memory_namespace(config)+".sqlite3"
        a.db = RUNTIME_DIR/name
    if a.port:
        from ripples.hardware.board import confirm_board
        confirm_board(a.port, "rgb565")
    visual = RipplesVisual(criticality=a.criticality)
    other = RipplesVisual("sleep", criticality=a.criticality) if a.preview else None
    active = RipplesVisual("activate") if a.preview else None
    preview = Preview(visual.waves.xyz, audio_status="LIVE: see input status", criticality=a.criticality,
                      live=True) if a.preview else None
    source = live_spikes() if a.reservoir == "live" else recorded_spikes()
    print("Preparing reservoir; live sensor clock starts after warmup.", flush=True)
    first = next(source)
    memory = None
    sensors = LiveSensors(config)
    interrupted = False
    def halt(*_):
        nonlocal interrupted
        interrupted = True
    previous_signal = signal.signal(signal.SIGINT, halt)
    stop_file = RUNTIME_DIR/"stop"
    stop_file.unlink(missing_ok=True)
    timings, intervals = deque(maxlen=10000), deque(maxlen=10000)
    encoder = InstallationEncoder()
    frames_done, started, last_report = 0, time.monotonic(), -10.
    report = {}
    state_status = state_control.at()

    def write_report():
        nonlocal last_report
        report.update(sensors.report(), reservoir=a.reservoir, neurons=8460, gain=9.5, dt_ms=.1,
                      config_file=str(a.config), memory_db=str(a.db) if memory else None,
                      accumulation_enabled=bool(memory),
                      display_state=state_status,
                      steps_per_frame=40, criticality=a.criticality, frames=frames_done,
                      wall_seconds=time.monotonic()-started, usb_requested=bool(a.port),
                      loop_fps=1/float(np.mean(intervals)) if intervals else 0,
                      render_encode_ms={k: float(v) for k, v in zip(("median", "p95", "max"),
                                        np.percentile(timings, [50, 95, 100])*1000)} if timings else {})
        a.report.parent.mkdir(parents=True, exist_ok=True)
        tmp = a.report.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(a.report)
        last_report = time.monotonic()

    def frames():
        nonlocal frames_done, state_status
        last, due, snapshot = None, time.monotonic(), None
        for raw, count in itertools.chain([first], source):
            if not a.port:
                time.sleep(max(0, due-time.monotonic()))
                due = time.monotonic()+1/a.fps
            now = time.monotonic()
            if interrupted or stop_file.exists() or (a.seconds and now-started >= a.seconds):
                break
            dt = now-last if last is not None else 1/a.fps
            if last is not None:
                intervals.append(dt)
            last = now
            if memory and memory.error:
                raise RuntimeError(f"spatial memory stopped: {memory.error}")
            current = memory.snapshot if memory else None
            if current is not None and current is not snapshot:
                visual.memory, visual.memory_spread = current
                if other:
                    other.memory, other.memory_spread = current
                snapshot = current
            # Switch only the renderer. Input threads, model and memory stay alive.
            previous_state = state_status["state"]
            state_status = state_control.at()
            visual.set_state(state_status["state"])
            if state_status["state"] != previous_state:
                print(json.dumps({"state_transition": state_status}, ensure_ascii=False), flush=True)
            batch = sensors.drain()
            tick = time.perf_counter()
            frame = visual.render(raw, count, events=batch, dt=dt)
            if not a.port:
                encoder.encode_packet(frame, frames_done)
            timings.append(time.perf_counter()-tick)
            if preview:
                af = active.render(raw, count, events=batch, dt=dt)
                sf = other.render(raw, count, events=batch, dt=dt)
                if frames_done % 2 == 0:
                    preview.add(af, sf, now-started)
            frames_done += 1
            if now-last_report >= 5:
                write_report()
                print(json.dumps(report, ensure_ascii=False), flush=True)
            yield frame

    try:
        if position_enabled(config) and a.accumulate:
            memory = LiveMemory(a.db, config)
            sensors.memory = memory
            print(f"Position mode: {config.get('position_mode', 'range_fit')}; memory: {a.db}", flush=True)
        elif not position_enabled(config):
            print("UNCALIBRATED: receiving and preprocessing only; no invented XY or measured DB writes.", flush=True)
        else:
            print("Accumulation disabled; current sensor display only (no spatial DB opened).", flush=True)
        sensors.start_center(a.center, tof=not a.collector_port, distance=not a.collector_port, mic=not a.audio_port)
        if a.collector_port:
            sensors.start_collector(a.bind, a.collector_port)
        if a.audio_port:
            sensors.start_collector(a.bind, a.audio_port)
        if a.port:
            from ripples.hardware.transport import LEDUSB
            from ripples.spikes import send_frames
            with LEDUSB(a.port, version="rgb565") as link:
                report["acknowledged_frames"] = send_frames(link, frames(), a.fps)
        else:
            for _ in frames():
                pass
    finally:
        signal.signal(signal.SIGINT, previous_signal)
        sensors.close()
        if memory:
            memory.close()
        source.close()
        write_report()
    if preview:
        preview.save(a.preview, a.fps)
    print(f"Live status: {a.report}", flush=True)


if __name__ == "__main__":
    main()
