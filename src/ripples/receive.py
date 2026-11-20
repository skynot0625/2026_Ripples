"""Connectivity check only: LAN data in, fixed health status out. No DB/model/LED."""
import argparse
import json
from pathlib import Path
import signal
import time

import numpy as np

from ripples.paths import ROOT, RUNTIME_DIR
from ripples.realtime import LiveSensors, validate_config


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--center", default="http://127.0.0.1:8000")
    p.add_argument("--config", type=Path, default=ROOT/"configs/live.m30.json")
    p.add_argument("--seconds", type=float, default=30, help="0=continuous until Ctrl+C")
    p.add_argument("--report", type=Path, default=RUNTIME_DIR/"receiver-status.json")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--collector-port", type=int)
    p.add_argument("--audio-port", type=int)
    a = p.parse_args()
    if not np.isfinite(a.seconds) or a.seconds < 0:
        p.error("seconds must be finite and >=0")
    if any(port is not None and not 1024 <= port <= 65535 for port in (a.collector_port, a.audio_port)):
        p.error("ports must be 1024..65535")
    if a.collector_port and a.collector_port == a.audio_port:
        p.error("use separate collector/audio ports")
    config = validate_config(json.loads(a.config.read_text(encoding="utf-8-sig")))
    sensors = LiveSensors(config, memory=None)
    interrupted = False
    def halt(*_):
        nonlocal interrupted
        interrupted = True
    previous = signal.signal(signal.SIGINT, halt)
    start, last_report = time.monotonic(), -10.
    def report():
        nonlocal last_report
        doc = dict(sensors.report(), mode="receive_only", accumulation_enabled=False,
                   memory_namespace=None, memory_db=None, reservoir_started=False, usb_opened=False,
                   wall_seconds=round(time.monotonic()-start, 3))
        a.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = a.report.with_suffix(".tmp")
        temporary.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(a.report)
        last_report = time.monotonic()
        print(json.dumps({"mode": doc["mode"], "counts": doc["counts"], "errors": doc["errors"],
                          "positions_available": doc["positions_available"]}, ensure_ascii=False), flush=True)
    try:
        sensors.start_center(a.center, tof=not a.collector_port, distance=not a.collector_port, mic=not a.audio_port)
        if a.collector_port:
            sensors.start_collector(a.bind, a.collector_port)
        if a.audio_port:
            sensors.start_collector(a.bind, a.audio_port)
        while not interrupted and (not a.seconds or time.monotonic()-start < a.seconds):
            sensors.drain()  # do not retain features or traces between polls
            if time.monotonic()-last_report >= 5:
                report()
            time.sleep(.05)
    finally:
        sensors.close()
        signal.signal(signal.SIGINT, previous)
        report()


if __name__ == "__main__":
    main()
