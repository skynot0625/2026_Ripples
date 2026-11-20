"""Explicit synthetic microphone demo, isolated statistics and fair three-way preview."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time
import wave

import numpy as np
from PIL import Image, ImageDraw

from ripples.hardware.transport import VALID_MASK
from ripples.sensors import load_recordings
from ripples.memory import SpatialMemory
from ripples.visualization import RipplesVisual
from ripples.run import import_events, recorded_spikes
from ripples.paths import DATA_DIR, RUNTIME_DIR, OUTPUT_DIR


def prepare_demo(root):
    """Four laps produce measured variance of synthetic audio, not invented std."""
    folder = root/"input"
    folder.mkdir(parents=True, exist_ok=True)
    archives = sorted((DATA_DIR/"sensors").glob("ripples-hyeol-send-sample*.zip"))
    if not archives:
        raise ValueError("local Hyeol source ZIP missing")
    target = folder/archives[0].name
    if not target.exists():
        shutil.copy2(archives[0], target)
    rng = np.random.default_rng(20261120)
    for second in range(240):
        t = second+np.arange(44100)/44100.
        angle = 2*np.pi*t/60+np.pi
        # Persistent spatial character plus visit-to-visit differences.
        visit = .65+.55*rng.random(3)
        amplitudes = np.stack((.018+.13*(.5+.5*np.cos(angle))**2,
                               .015+.10*(.5+.5*np.sin(angle))**2,
                               .008+.08*(.5+.5*np.cos(angle+1.8))**3), axis=1)*visit
        amplitudes *= (1+.25*np.sin(t[:, None]*[.9, 1.4, 1.8]))
        channels = []
        for mic in range(3):
            tones = np.sin(2*np.pi*t[:, None]*[180., 950., 4800.]+mic*.8)
            channels.append(np.sum(amplitudes*tones, axis=1)*(1-.08*mic)+rng.normal(0, .001, len(t)))
        pcm = np.clip(np.column_stack(channels)*32767, -32768, 32767).astype("<i2")
        chunk = folder/"synthetic_maek"/f"{second:04d}"
        chunk.mkdir(parents=True, exist_ok=True)
        wav_path = chunk/"audio.wav"
        if not wav_path.exists():
            with wave.open(str(wav_path), "wb") as output:
                output.setparams((3, 2, 44100, 0, "NONE", "not compressed"))
                output.writeframes(pcm.tobytes())
        meta = {"device_id": "SYNTHETIC-MAEK", "session_id": "dream-demo-v1",
                "chunk_id": f"dream-v1-{second:04d}", "segment_index": second,
                "start_frame": second*44100, "frames": 44100, "demo": True,
                "sha256": hashlib.sha256(wav_path.read_bytes()).hexdigest()}
        (chunk/"metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


class CompareMovie:
    def __init__(self, xyz):
        xyz = xyz[VALID_MASK]
        xyz = (xyz-xyz.min(axis=0))/(xyz.max(axis=0)-xyz.min(axis=0))
        self.points = np.column_stack((22+xyz[:, 0]*285+xyz[:, 1]*48,
                                      415-xyz[:, 2]*280-xyz[:, 1]*30)).astype(int)
        self.order = np.argsort(xyz[:, 1])
        self.frames = []
        self.counts = []

    def add(self, frames, t, count, positions):
        image = Image.new("RGB", (1140, 590), (7, 11, 20))
        draw = ImageDraw.Draw(image)
        draw.text((20, 12), "RIPPLES / same recorded reservoir spikes in every panel / 8,460 neurons unchanged", fill=(230, 235, 245))
        draw.text((20, 33), "Real Hyeol + SYNTHETIC 3-channel Maek + synthetic positions. Sleep statistics: 4 audio laps.", fill=(145, 165, 185))
        labels = ("ACTIVATE: two fading traces", "SLEEP: mean + std + firing", "SLEEP: mean + std + slow waves")
        details = ("Cyan / purple = Hyeol; warm = Maek", "Actual firing pixels, 0.65s light decay", "Added geometry, triggered by SAME bursts")
        for panel, frame in enumerate(frames):
            offset = panel*380
            draw.text((offset+15, 65), labels[panel], fill=(220, 230, 240))
            draw.text((offset+15, 85), details[panel], fill=(140, 160, 185))
            colors = frame[VALID_MASK]
            for i in self.order:
                x, y = self.points[i]
                draw.point((int(x+offset), int(y)), fill=(18, 24, 33))
            for i in self.order[colors[self.order].max(axis=1) > 5]:
                x, y = self.points[i]
                color = tuple((255*(colors[i]/255.)**.7).astype(int))
                draw.ellipse((int(x+offset-1), int(y-1), int(x+offset+1), int(y+1)), fill=color)
        # A position map makes the two moving instruments unambiguous.
        draw.rectangle((20, 450, 140, 570), outline=(60, 75, 95))
        for source, xy in positions.items():
            x, y = 80+xy[0]*10, 510-xy[1]*10
            tint = (50, 210, 255) if source == "hyeol" else (255, 150, 60)
            draw.ellipse((x-4, y-4, x+4, y+4), fill=tint)
            draw.text((x+5, y-4), "H" if source == "hyeol" else "M", fill=tint)
        draw.text((160, 450), f"{t:05.1f}s | {'inputs off: Activate traces fade' if t >= 59.3 else 'both instruments moving'}", fill=(200, 215, 230))
        draw.text((160, 472), "Actual population spike count / 4 ms model window (fixed scale: 0..3000)", fill=(150, 170, 190))
        recent = self.counts[-600:]
        pts = [(162+j*1.5, 558-min(n/3000., 1)*58) for j, n in enumerate(recent)]
        if len(pts) > 1:
            draw.line(pts, fill=(120, 210, 190), width=2)
        draw.text((160, 572), "Std shimmer is a display effect. Radial waves are NOT proof of spatial critical propagation.", fill=(145, 165, 185))
        self.frames.append(image.convert("P", palette=Image.Palette.ADAPTIVE, colors=128))

    def save(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.frames[0].save(path, save_all=True, append_images=self.frames[1:], duration=200, loop=0, optimize=False)
        self.frames[len(self.frames)//2].convert("RGB").save(path.with_suffix(".png"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR/"dream_compare.gif")
    args = parser.parse_args()
    root = RUNTIME_DIR/"dream-demo"
    folder = prepare_demo(root)
    events, _, info = load_recordings(folder)
    memory = SpatialMemory(root/"dream.sqlite3", namespace="demo")
    try:
        info["new_batches"] = import_events(memory, events)
        snapshot = memory.snapshot()
        info["storage"] = memory.report()
    finally:
        memory.close()
    visuals = [RipplesVisual("activate"), RipplesVisual("sleep", criticality="activity"),
               RipplesVisual("sleep", criticality="slow_waves")]
    for visual in visuals:
        visual.set_memory(snapshot)
    movie = CompareMovie(visuals[0].waves.xyz)
    source = recorded_spikes()
    # Show one lap and 8 seconds with no new sensing to prove Activate fades.
    playback = [e for e in events if e["t"] < 59.3]
    index = 0
    positions, timings = {}, []
    for frame in range(680):
        t = frame/10.
        raw, count = next(source)
        batch = []
        while index < len(playback) and playback[index]["t"] <= t:
            batch.append(playback[index])
            positions[playback[index]["source"]] = playback[index]["xy"]
            index += 1
        start = time.perf_counter()
        rendered = [v.render(raw, count, events=batch, dt=.1) for v in visuals]
        timings.append(time.perf_counter()-start)
        movie.counts.append(int(count))
        if frame % 2 == 0:
            movie.add(rendered, t, int(count), positions)
    source.close()
    movie.save(args.output)
    info.update(preview_seconds=68, preview_fps=5, render_fps=10,
                activate_fully_dark_at_end=not bool(rendered[0].any()),
                three_panels_render_ms_p95=float(np.percentile(timings, 95)*1000),
                cells_with_nonzero_std=sum(any(s > 0 for s in e["std"].values()) for e in snapshot),
                synthetic_audio_seconds=240, neurons=8460, gain=9.5,
                wave_time_scale=.30, noise_time_constant_s=1.8)
    (root/"comparison.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(json.dumps(info, indent=2), flush=True)


if __name__ == "__main__":
    main()
