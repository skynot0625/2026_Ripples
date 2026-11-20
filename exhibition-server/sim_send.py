#!/usr/bin/env python3
"""Example sender: push 심 LED frames to the server so /sim shows them (pattern "AI 입력").

    python sim_send.py                      # zone colours (check the port map against the drawing), 20 fps
    python sim_send.py --demo ball          # a ball moving through the volume
    python sim_send.py --url http://192.168.0.2:8000

Frame = numpy uint8 (12, 16, 64, 3): [zone − 1, port − 1, LED from the top (LED 1 in the hole not included), RGB].
Send it as raw bytes: POST /api/sim/frame (Content-Type application/octet-stream), or binary messages on ws /ws/sim/in.
Unused ports / LEDs past the end of a strip are ignored (leave them 0).
"""

from __future__ import annotations

import argparse
import colorsys
import csv
import math
import time
import urllib.request
from pathlib import Path

import numpy as np

SHAPE = (12, 16, 64, 3)
LED_PITCH = 0.05  # m


def load_strips(path: Path) -> list[dict]:
    rows = [r for r in csv.DictReader(open(path, encoding="utf-8")) if r["included_in_layout"] == "True"]
    # x, y in sim_mapping.csv are in hole pitches (2120/17 mm); lit LEDs = led_count − 1 (LED 1 sits in the hole)
    return [{"name": r["position_id"], "zone": int(r["zone"]), "port": int(r["port_c"]), "n": int(r["led_count"]) - 1,
             "x": float(r["x"]) * 0.1247, "y": float(r["y"]) * 0.1247} for r in rows]


def frame_zones(strips: list[dict], t: float) -> np.ndarray:
    f = np.zeros(SHAPE, np.uint8)
    for s in strips:
        r, g, b = colorsys.hsv_to_rgb((s["zone"] - 1) / 12, 0.8, 1.0)
        f[s["zone"] - 1, s["port"] - 1, : s["n"]] = (np.array([r, g, b]) * 200).astype(np.uint8)
        # a dot running down each strip, offset by port, to show the LED order (top = index 0)
        k = int((t * 20 + s["port"] * 3) % s["n"])
        f[s["zone"] - 1, s["port"] - 1, k] = 255
    return f


def frame_ball(strips: list[dict], t: float) -> np.ndarray:
    f = np.zeros(SHAPE, np.uint8)
    cx, cy, cd = 0.7 * math.sin(t * 0.7), 0.6 * math.cos(t * 0.5), 1.4 + 0.8 * math.sin(t * 0.9)   # cd: depth below the hole (m)
    depth = (np.arange(64) + 1) * LED_PITCH   # LED k (index 0 = LED 2) is (k + 1) × 5 cm below the hole
    for s in strips:
        d = np.sqrt((s["x"] - cx) ** 2 + (s["y"] - cy) ** 2 + (depth - cd) ** 2)
        a = np.clip(1 - np.abs(d - 0.45) / 0.08, 0, 1)
        a[s["n"]:] = 0
        f[s["zone"] - 1, s["port"] - 1] = (a[:, None] * np.array([255, 160, 60])).astype(np.uint8)
    return f


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--demo", choices=("zones", "ball"), default="zones")
    ap.add_argument("--fps", type=float, default=20)
    ap.add_argument("--seconds", type=float, default=0, help="0 = until Ctrl+C")
    args = ap.parse_args()
    strips = load_strips(Path(__file__).with_name("sim_mapping.csv"))
    make = frame_zones if args.demo == "zones" else frame_ball
    t0 = time.time()
    n = 0
    while not args.seconds or time.time() - t0 < args.seconds:
        frame = make(strips, time.time() - t0)
        req = urllib.request.Request(args.url.rstrip("/") + "/api/sim/frame", data=frame.tobytes(), method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        urllib.request.urlopen(req, timeout=2).read()
        n += 1
        time.sleep(max(0.0, t0 + n / args.fps - time.time()))
    print(f"sent {n} frames")


if __name__ == "__main__":
    main()
