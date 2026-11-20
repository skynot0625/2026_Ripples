"""Export logical RGB for venue3d; no USB color swaps or physical transmissions."""
import base64
import csv
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

from ripples.hardware.transport import VALID_MASK
from ripples.paths import LAYOUT_DIR


def recorded_activity_clip(frames=680):
    """Inline bounded, recorded firing-only layer for browser position simulation."""
    from ripples.run import recorded_spikes
    source = recorded_spikes()
    activity = np.zeros((*VALID_MASK.shape,3), np.float32)
    chunks = []
    try:
        for _ in range(frames):
            raw, _ = next(source)
            activity *= np.exp(-.1/.65)
            activity = np.maximum(activity, raw.astype(np.float32))
            chunks.append(activity[VALID_MASK].astype(np.uint8).tobytes())
    finally:
        source.close()
    compressed = gzip.compress(b"".join(chunks), compresslevel=6, mtime=0)
    return {"encoding":"gzip+base64", "frames":frames, "fps":10, "neurons":8460,
            "channels":3, "source":"recorded reservoir gain9.5; firing + 0.65s afterglow only",
            "sha256":hashlib.sha256(compressed).hexdigest(),
            "data":base64.b64encode(compressed).decode("ascii")}


class ExampleClip:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.frames = 0
        self.files = {state: self.folder/f"ripples-{state}.rgb.gz" for state in ("activate", "sleep")}
        self.streams = {state: gzip.open(path, "wb", compresslevel=6) for state, path in self.files.items()}

    def add(self, frames):
        if len(frames) != 2:
            raise ValueError("Activate and Sleep frames required")
        for state, frame in zip(self.streams, frames):
            if frame.shape != (*VALID_MASK.shape, 3) or frame.dtype != np.uint8:
                raise ValueError("invalid logical RGB frame")
            self.streams[state].write(frame[VALID_MASK].tobytes())
        self.frames += 1

    def close(self, bounds, *, include_activity=False):
        for stream in self.streams.values():
            stream.close()
        offsets = np.cumsum(np.r_[0, VALID_MASK.sum(axis=2).ravel()[:-1]])
        strips = {}
        with (LAYOUT_DIR/"mapping_plus10.csv").open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row["included_in_layout"] != "True":
                    continue
                z, p = int(row["zone"])-1, int(row["port_c"])-1
                strips[row["position_id"]] = {"offset": int(offsets[z*16+p]),
                    "count": int(VALID_MASK[z,p].sum()), "physical_visible_count": int(row["led_count"])-1}
        doc = {"schema": "ripples.venue-example/1", "example": True,
               "mapping": "sensory-v2", "sleep_style": "dream-v2",
               "sleep_memory_gain": .72, "dream_tau_s": 3.2, "fps": 10, "frames": self.frames,
               "neurons": int(VALID_MASK.sum()), "channels": 3, "duration_s": self.frames/10,
               "history_s": 180, "input_end_s": 60, "led_bounds_m": np.asarray(bounds).tolist(),
               "added_radial_waves": False, "logical_rgb": True, "strips": strips,
               "geometry_note": "Resample each named strip onto venue3d's existing geometry; not a hardware count change.",
               "files": {state: {"name": path.name, "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "decoded_bytes": self.frames*int(VALID_MASK.sum())*3}
                         for state, path in self.files.items()}}
        if include_activity:
            doc["activity_clip"] = recorded_activity_clip(self.frames)
        (self.folder/"ripples-example.json").write_text(json.dumps(doc, indent=2)+"\n", encoding="utf-8")
        return doc
