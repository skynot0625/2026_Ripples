"""M30 scene -> explicitly synthetic sensor forms -> causal Activate/Sleep GIF.

Offline only. No HTTP writes, USB, live receiver, or operational DB access.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import heapq
import json
from pathlib import Path
import struct
import tempfile
import time

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import distance_transform_edt, label

from ripples.demo import CompareMovie
from ripples.example_form import VERSION, load_events, schema, to_render_event, validate
from ripples.memory import SpatialMemory
from ripples.paths import DATA_DIR, ROOT, RUNTIME_DIR
from ripples.realtime import Positions
from ripples.run import recorded_spikes
from ripples.sensors import depth_features
from ripples.visualization import RipplesVisual
from ripples.example_clip import ExampleClip

FOLDER = DATA_DIR/"m30_session"
SEED = 20261120
BASE_NS = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp()*1e9)
BLOCKS = {"column", "wall", "glass", "door", "movable_wall", "fixture", "mock_frame", "mock_plate"}


class Scene:
    def __init__(self, doc, spacing=.10):
        self.doc, self.spacing = doc, spacing
        if doc["units"] != "mm" or doc["id"] != "m30":
            raise ValueError("M30 mm scene required")
        b = doc["bounds"]
        self.bounds = np.array([[b["x0"], b["x1"]], [b["y0"], b["y1"]]])/1000
        self.led_bounds = np.asarray(doc.get("led_mapping", {}).get("bounds_m", self.bounds), dtype=float)
        if (self.led_bounds.shape != (2, 2) or not np.isfinite(self.led_bounds).all()
                or np.any(self.led_bounds[:, 0] < self.bounds[:, 0])
                or np.any(self.led_bounds[:, 1] > self.bounds[:, 1])
                or np.any(self.led_bounds[:, 1] <= self.led_bounds[:, 0])):
            raise ValueError("invalid LED mapping viewport")
        size = np.ceil(np.diff(self.bounds, axis=1).ravel()/spacing).astype(int)+1
        im = Image.new("L", tuple(size), 0)
        draw = ImageDraw.Draw(im)
        draw.polygon(self.pixels(doc["walkable"]), fill=1)
        for solid in doc["solids"]:
            if solid["kind"] in BLOCKS and solid.get("z0", 0) < 1500:
                draw.polygon(self.pixels(solid["poly"]), fill=0)
        # The tower footprint is not a route for a moving sensing instrument.
        tower = doc["layout"]["sim"]
        x, y = self.grid([tower["x"]/1000, tower["y"]/1000])
        r = 1.1/spacing
        draw.ellipse((x-r, y-r, x+r, y+r), fill=0)
        self.open = np.array(im, dtype=bool)
        self.clearance = distance_transform_edt(self.open)*spacing
        components, _ = label(self.clearance >= .7)
        candidates = np.bincount(components.ravel()); candidates[0] = 0
        if not candidates.max():
            raise ValueError("no connected free space with instrument clearance")
        self.safe = components == candidates.argmax()
        self.route = self.build_route()
        self.cumulative = np.r_[0, np.cumsum(np.linalg.norm(np.diff(self.route, axis=0), axis=1))]

    def pixels(self, points_mm):
        a = (np.asarray(points_mm)/1000-self.bounds[:, 0])/self.spacing
        return [tuple(p) for p in np.rint(a).astype(int)]

    def grid(self, xy):
        return tuple(np.rint((np.asarray(xy)-self.bounds[:, 0])/self.spacing).astype(int))

    def point(self, node):
        return np.asarray(node)*self.spacing+self.bounds[:, 0]

    def nearest(self, xy):
        ys, xs = np.nonzero(self.safe)
        points = np.column_stack((xs, ys))
        return tuple(points[np.argmin(np.sum((points-self.grid(xy))**2, axis=1))])

    def path(self, start, end):
        costs, previous, todo = {start: 0}, {}, [(0, start)]
        while todo:
            _, node = heapq.heappop(todo)
            if node == end:
                route = [end]
                while route[-1] != start:
                    route.append(previous[route[-1]])
                return route[::-1]
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = node[0]+dx, node[1]+dy
                x, y = nxt
                if not (0 <= y < self.safe.shape[0] and 0 <= x < self.safe.shape[1] and self.safe[y, x]):
                    continue
                cost = costs[node]+1
                if cost < costs.get(nxt, float("inf")):
                    costs[nxt], previous[nxt] = cost, node
                    heapq.heappush(todo, (cost+abs(x-end[0])+abs(y-end[1]), nxt))
        raise ValueError("disconnected route")

    def build_route(self):
        nodes = [self.nearest(p) for p in ((3, 3), (8, 5), (9, 17), (3, 18))]
        route = []
        for start, end in zip(nodes, nodes[1:]+nodes[:1]):
            route.extend(self.path(start, end)[:-1])
        if len(set(route)) < 20:
            raise ValueError("scene has insufficient free route")
        route.append(route[0])
        return np.asarray([self.point(p) for p in route])

    def pose(self, seconds, phase=0):
        distance = ((seconds/60+phase) % 1)*self.cumulative[-1]
        i = min(len(self.route)-2, np.searchsorted(self.cumulative, distance, side="right")-1)
        mix = (distance-self.cumulative[i])/(self.cumulative[i+1]-self.cumulative[i])
        xy = self.route[i]*(1-mix)+self.route[i+1]*mix
        delta = self.route[i+1]-self.route[i]
        return xy, np.arctan2(delta[0], delta[1])

    def ray(self, xy, angle):
        distances = np.arange(.05, 4.001, .05)
        p = xy+distances[:, None]*[np.sin(angle), np.cos(angle)]
        ij = np.rint((p-self.bounds[:, 0])/self.spacing).astype(int)
        for d, (x, y) in zip(distances, ij):
            if not (0 <= y < self.open.shape[0] and 0 <= x < self.open.shape[1]) or not self.open[y, x]:
                return d
        return float("inf")


def pack(header, payload=b""):
    blob = json.dumps(header, separators=(",", ":"), allow_nan=False).encode()
    return struct.pack("<II", len(blob), len(payload))+blob+payload


def write_json(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def camera(scene, xy, heading, t, rng):
    depth, conf = np.zeros((180, 240), "<u2"), np.zeros((180, 240), "<u2")
    for row, pitch in enumerate((20, 0, -20)):
        rad = np.deg2rad(pitch)
        for col, angle in enumerate((-25, 0, 25)):
            distance = scene.ray(xy, heading+np.deg2rad(angle))/np.cos(rad)
            if pitch < 0:
                distance = min(distance, 1/(-np.sin(rad)))  # scene ToF height = 1m
            if distance <= 4:
                distance = np.clip(distance+.025*np.sin(1.3*t+row+col)+rng.normal(0, .015), .05, 4)
                patch = depth[row*60:(row+1)*60, col*80:(col+1)*80]
                patch[:] = round(distance*1000)
                quality = .82+.12*np.sin(t*.3+col+row)**2
                cp = np.zeros(4800, "<u2"); cp[:round(quality*4800)] = 100
                conf[row*60:(row+1)*60, col*80:(col+1)*80] = cp.reshape(60, 80)
    # Mimic upside-down raw camera; shared decoder performs the only rotation.
    payload = depth[::-1, ::-1].tobytes()+conf[::-1, ::-1].tobytes()
    h = {"type": "tof", "seq": round(t*10), "t_wall_ns": BASE_NS+round(t*1e9),
         "t_mono_ns": 1_000_000_000+round(t*1e9), "width": 240, "height": 180,
         "depth_dtype": "<u2", "conf_dtype": "<u2"}
    f = depth_features(h, payload, rotate=180, confidence=60)
    return h, payload, {"depth_m": [f.get(f"depth_{i}") for i in range(9)],
                        "coverage": [f[f"coverage_{i}"] for i in range(9)],
                        "upward_m": float(round(2.2+.035*np.sin(.7*t), 4))}


def generate(folder=FOLDER):
    scene = Scene(json.loads((folder/"venue.json").read_text(encoding="utf-8")))
    config = json.loads((ROOT/"configs/live.m30.json").read_text(encoding="utf-8"))
    if not np.allclose(config["bounds_m"], scene.led_bounds):
        raise ValueError("scene/site coordinate bounds differ")
    rng, positions = np.random.default_rng(SEED), Positions(config)
    events, wires, audio = [], [], []
    beacon = {}
    for tick in range(2400):
        t = tick/10
        poses = {role: scene.pose(t, phase) for role, phase in (("hyeol", 0), ("maek", .5))}
        if tick % 10 == 0:
            beacon = {}
            for role, (xy, _) in poses.items():
                nodes = {}
                for key, a in config["anchors"].items():
                    distance = max(.3, np.linalg.norm(xy-a["xy_m"]))
                    rssi = round(-59-40*np.log10(distance)+rng.normal(0, .5), 2)
                    nodes[key] = {"state": "OK", "median_dbm": rssi, "last_dbm": rssi,
                                  "n": 10, "age_ms": 0, "hardware_id": "EXAMPLE-"+key, "reference_rssi_dbm": None}
                body = {"v": 1, "type": "beacon", "receiver": config["receivers"][role], "seq": tick//10,
                        "t_wall_ns": BASE_NS+round(t*1e9), "window_s": 1.5, "nodes": nodes}
                wires.append(body)
                beacon[body["receiver"]] = dict(body, received_ago_ms=0)
            positions.update({"receivers": beacon}, now=t)
        for role, (xy, heading) in poses.items():
            estimated = positions.report()[role]["xy_m"]
            if role == "hyeol":
                header, payload, m = camera(scene, xy, heading, t, rng)
                if tick == 0:
                    lid = {"type": "lidar", "seq": 0, "t_wall_ns": BASE_NS, "t_mono_ns": 1_000_000_000,
                           "id": 0, "distance_mm": round(m["upward_m"]*1000), "status": 0, "valid": True, "strength": 1000}
                    hello = {"type": "hello", "version": 1, "rotate": 180,
                             "tof": {"width": 240, "height": 180, "range_mm": 4000}, "lidar": {"model": "EXAMPLE"}}
                    (folder/"hyeol_sample.bin").write_bytes(pack(hello)+pack(header, payload)+pack(lid))
            else:
                d = min(np.linalg.norm(xy-[3, 5]), np.linalg.norm(xy-[8, 16]))
                level = (.001+.028/(1+.15*d*d))*(.8+.2*np.sin(t*1.7))
                rms = np.round(level*np.array([.9+.1*np.sin(t), 1, .9-.1*np.sin(t)]), 6)
                m = {"rms": rms.tolist(), "mics": np.round(np.clip((20*np.log10(rms)+70)/50, 0, 1), 4).tolist()}
                audio.append({"v": 1, "device": "EXAMPLE-MAEK", "seq": tick, "t": BASE_NS/1e9+t,
                              **m, "haptic": [0, 0], "level_bias": 0., "direction_deg": None})
            event = {"schema": VERSION, "example": True, "session_id": "m30-example-v1", "source": role,
                     "seq": tick, "t_ms": tick*100,
                     "position": {"frame": "m30", "method": "simulation_truth", "xy_m": np.round(xy, 5).tolist(),
                                  "relative_xy_m": np.round(estimated, 5).tolist(),
                                  "beacon_seq": tick//10, "beacon_age_ms": tick % 10*100}, "measurement": m}
            events.append(validate(event))
    for name, records in (("events.jsonl", events), ("beacon_wire.jsonl", wires), ("maek_wire.jsonl", audio)):
        (folder/name).write_text("".join(json.dumps(e, separators=(",", ":"), allow_nan=False)+"\n" for e in records), encoding="utf-8")
    for role in ("hyeol", "maek"):
        write_json(folder/f"sample_{role}.json", next(e for e in events if e["source"] == role))
    write_json(folder/"event.schema.json", schema())
    write_json(folder/"route.json", {"xy_m": scene.route.tolist(), "length_m": float(scene.cumulative[-1]),
                                     "period_s": 60, "approx_clearance_m": .7, "grid_m": scene.spacing})
    names = ("venue.json", "events.jsonl", "beacon_wire.jsonl", "maek_wire.jsonl", "hyeol_sample.bin",
             "sample_hyeol.json", "sample_maek.json", "event.schema.json", "route.json")
    write_json(folder/"manifest.json", {"schema": "ripples.example-session/1", "example": True, "seed": SEED,
               "venue": "actual M30 scene snapshot; all movements and sensor signals are synthetic",
               "position_for_led": "simulation_truth", "relative_rssi_position": "diagnostic only, not mapped into LED",
               "duration_s": 240, "rate_hz_per_source": 10, "events": len(events),
               "mapping": {"bounds_m": scene.led_bounds.tolist(), "region": "M30 right exhibition room, lobby excluded",
                           "axis_rule": "+LED X/Y = +M30 X/Y",
                           "tower_heading_applied": False, "position_truth_used_only_for_example": True},
               "units": {"position": "m", "depth": "m", "rssi": "dBm", "time": "session-relative ms", "rms": "digital full scale"},
               "files": {name: {"bytes": (folder/name).stat().st_size, "sha256": hashlib.sha256((folder/name).read_bytes()).hexdigest()} for name in names}})
    return scene


class VenueMovie(CompareMovie):
    def __init__(self, xyz, scene):
        super().__init__(xyz, subtitle="SYNTHETIC sensor session on actual M30 layout. Sleep uses only earlier example seconds.", input_end_s=60)
        self.scene = scene
        self.map = Image.new("RGB", (410, 590), (7, 11, 20))
        draw = ImageDraw.Draw(self.map)
        draw.text((18, 12), "MULLAE M30 | venue to LED XY", fill=(230, 235, 245))
        draw.text((18, 34), "Actual plan / synthetic moving instruments", fill=(145, 165, 185))
        self.scale = min(370/np.ptp(scene.bounds[0]), 390/np.ptp(scene.bounds[1]))
        draw.polygon([self.pixel(np.array(p)/1000) for p in scene.doc["walkable"]], fill=(22, 30, 42), outline=(75, 95, 115))
        for solid in scene.doc["solids"]:
            if solid["kind"] in BLOCKS:
                draw.polygon([self.pixel(np.array(p)/1000) for p in solid["poly"]], fill=(75, 80, 90))
        draw.line([self.pixel(p) for p in scene.route], fill=(48, 65, 80), width=1)
        for key, a in scene.doc["layout"]["beacons"].items():
            x, y = self.pixel([a["x"]/1000, a["y"]/1000])
            draw.rectangle((x-3, y-3, x+3, y+3), fill=(220, 195, 95))
            draw.text((x+5, y-8), key, fill=(230, 210, 120))
        tower = scene.doc["layout"]["sim"]
        x, y = self.pixel([tower["x"]/1000, tower["y"]/1000])
        draw.rectangle((x-9, y-9, x+9, y+9), outline=(200, 190, 225), width=2)
        draw.text((x+10, y-6), "SIM", fill=(200, 190, 225))
        # Architectural solids can extend beyond walkable bounds; never let them
        # overwrite the legend or change the world-to-map scale.
        right = round(18+np.ptp(scene.bounds[0])*self.scale)
        bottom = round(82+np.ptp(scene.bounds[1])*self.scale)
        region = self.map.crop((18, 82, right+1, bottom+1))
        self.map = Image.new("RGB", (410, 590), (7, 11, 20))
        self.map.paste(region, (18, 82))
        draw = ImageDraw.Draw(self.map)
        x0, y0 = self.pixel([scene.led_bounds[0, 0], scene.led_bounds[1, 1]])
        x1, y1 = self.pixel([scene.led_bounds[0, 1], scene.led_bounds[1, 0]])
        draw.rectangle((x0, y0, x1, y1), outline=(70, 220, 180), width=2)
        draw.text((x0+4, y0+5), "LED XY AREA", fill=(110, 240, 200))
        draw.text((18, 12), "MULLAE M30 | venue to LED XY", fill=(230, 235, 245))
        draw.text((18, 34), "Actual plan / synthetic moving instruments", fill=(145, 165, 185))
        draw.text((18, 462), "Dots H/M: known simulator XY -> LED XY", fill=(180, 195, 210))
        draw.text((18, 482), "Hollow dots: uncalibrated RSSI estimate", fill=(145, 165, 185))
        draw.text((18, 502), "Gold 1..5: confirmed beacons; grey: walls", fill=(145, 165, 185))
        draw.text((18, 522), "Green box: right room -> full LED XY extent.", fill=(110, 220, 180))
        draw.text((18, 542), "LED height = sensor feature, not floor height.", fill=(145, 165, 185))
        draw.text((18, 572), "Example only | no live DB writes or LED output", fill=(125, 190, 175))

    def pixel(self, xy):
        return (18+(xy[0]-self.scene.bounds[0, 0])*self.scale,
                82+(self.scene.bounds[1, 1]-xy[1])*self.scale)

    def add_venue(self, frames, t, count, positions, latest):
        super().add(frames, t, count, positions)
        leds = self.frames.pop().convert("RGB")
        im = Image.new("RGB", (1170, 590)); im.paste(self.map, (0, 0)); im.paste(leds, (410, 0))
        draw = ImageDraw.Draw(im)
        for role, e in latest.items():
            color = (40, 215, 255) if role == "hyeol" else (255, 205, 125)
            xy = e["position"]["xy_m"]
            x, y = self.pixel(xy)
            ex, ey = self.pixel(e["position"]["relative_xy_m"])
            draw.line((x, y, ex, ey), fill=(65, 85, 95))
            draw.ellipse((ex-4, ey-4, ex+4, ey+4), outline=color)
            draw.ellipse((x-5, y-5, x+5, y+5), fill=color)
            draw.text((x+7, y-7), "H" if role == "hyeol" else "M", fill=color)
        self.frames.append(im.convert("P", palette=Image.Palette.ADAPTIVE, colors=128))


def render(folder, scene, output):
    events = load_events(folder/"events.jsonl")
    normalized = [to_render_event(e, scene.led_bounds) for e in events]
    seconds = defaultdict(list)
    for e in normalized:
        seconds[int(e["t"])].append(e)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="m30-example-", dir=RUNTIME_DIR) as temporary:
        assert Path(temporary).resolve().is_relative_to(RUNTIME_DIR)
        memory = SpatialMemory(Path(temporary)/"example.sqlite3", namespace="demo")
        clip = ExampleClip(ROOT/"exhibition-server/static")
        try:
            for s in range(180):
                memory.import_recording(f"example:{s}", seconds[s], epoch=BASE_NS/1e9)
            active, sleep = RipplesVisual("activate", output_limits=False), RipplesVisual("sleep", criticality="activity", output_limits=False)
            sleep.set_memory(memory.snapshot())
            movie, source = VenueMovie(active.waves.xyz, scene), recorded_spikes()
            displayed = [e for e in events if e["t_ms"] >= 180000]
            idx, latest, places, timing = 0, {}, {}, []
            for frame in range(680):
                t = frame/10
                if frame and frame % 10 == 0 and t <= 60:
                    s = 180+frame//10-1
                    memory.import_recording(f"example:{s}", seconds[s], epoch=BASE_NS/1e9)
                    sleep.set_memory(memory.snapshot())
                batch = []
                while idx < len(displayed) and displayed[idx]["t_ms"] <= 180000+frame*100:
                    e = displayed[idx]; idx += 1
                    latest[e["source"]] = e
                    r = to_render_event(e, scene.led_bounds); batch.append(r); places[e["source"]] = r["xy"]
                raw, count = next(source)
                begin = time.perf_counter()
                images = [active.render(raw, count, events=batch, dt=.1), sleep.render(raw, count, dt=.1)]
                timing.append(time.perf_counter()-begin)
                clip.add(images)
                movie.counts.append(count)
                if frame % 2 == 0:
                    movie.add_venue(images, t, count, places, latest)
            source.close()
            clip_doc = clip.close(scene.led_bounds)
            movie.save(output)
            snapshot = memory.snapshot()
            report = {"example": True, "venue": "m30", "history_s": 180, "visible_input_s": 60, "fade_s": 8,
                      "gif_fps": 5, "led_render_fps": 10, "neurons": 8460, "gain": 9.5,
                      "position_mode": "simulation_truth", "added_radial_waves": False,
                      "sensor_mapping": "sensory-v2", "sleep_style": "dream-v2",
                      "sleep_memory_gain": .72, "dream_tau_s": 3.2, "venue3d_clip_frames": clip_doc["frames"],
                      "led_mapping_bounds_m": scene.led_bounds.tolist(), "lobby_mapped": False,
                      "active_dark_at_end": not bool(images[0].any()), "spatial_cells": len(snapshot),
                      "cells_with_std": sum(any(v > 0 for v in e["std"].values()) for e in snapshot),
                      "render_p95_ms": float(np.percentile(timing, 95)*1000),
                      "operational_db_accessed": False, "temporary_example_db_removed": True,
                      "route_length_m": float(scene.cumulative[-1]), "route_speed_m_s": float(scene.cumulative[-1]/60)}
        finally:
            for stream in clip.streams.values():
                stream.close()
            memory.close()
    write_json(output.with_suffix(".json"), report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--folder", type=Path, default=FOLDER)
    p.add_argument("--output", type=Path, default=ROOT/"docs/media/m30_active_sleep.gif")
    a = p.parse_args()
    scene = generate(a.folder)
    print(json.dumps(render(a.folder, scene, a.output), indent=2), flush=True)


if __name__ == "__main__":
    main()
