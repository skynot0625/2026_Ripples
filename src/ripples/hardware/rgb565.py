from ripples.paths import LAYOUT_DIR
"""2026-11-12 installation, LED2 RGB565, 65 visible slots per connector."""
import csv
import json
from pathlib import Path

import numpy as np
from ripples.hardware.transport import HEADER, VALID_MASK, rgb565_pack, rgb565_unpack
import zlib


class InstallationEncoder:
    def __init__(self, *, zone_to_unit=tuple(range(12))):
        root = LAYOUT_DIR
        swap = set(json.loads((root / "color_corrections_plus10.json").read_text())["swap_red_green"])
        self.corrections = []
        self.zone_to_unit = np.asarray(zone_to_unit)
        if sorted(self.zone_to_unit.tolist()) != list(range(12)):
            raise ValueError("Invalid Zone to unit mapping")
        self.source = np.zeros((12, 16, 65), dtype=int)
        self.mask = np.zeros_like(self.source, dtype=bool)
        with (root / "mapping_plus10.csv").open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row["included_in_layout"] != "True":
                    continue
                z, p = int(row["zone"])-1, int(row["port_c"])-1
                n = int(row["led_count"])-1
                old = int(VALID_MASK[z,p].sum())
                if n != old + 10:
                    raise ValueError("Installation length mismatch")
                self.source[z,p,:n] = np.rint(np.linspace(0, old-1, n)).astype(int)
                self.mask[z,p,:n] = True
                if row["position_id"] in swap:
                    self.corrections.append((z,p))
        if self.mask.sum() != 10260 or len(self.corrections) != 34:
            raise ValueError("Installation layout mismatch")

    def prepare(self, frame):
        rgb = np.asarray(frame)
        if rgb.shape == VALID_MASK.shape:
            rgb = np.repeat(rgb[..., None], 3, axis=-1)
        if rgb.shape != (*VALID_MASK.shape,3) or rgb.dtype != np.uint8:
            raise ValueError("Expected a uint8 reservoir frame")
        result = np.take_along_axis(rgb, self.source[..., None], axis=2)
        result[~self.mask] = 0
        return result

    def preview_frame(self, frame):
        rgb = rgb565_unpack(rgb565_pack(self.prepare(frame)))
        return ((rgb.astype(np.uint16)*33)>>8).astype(np.uint8)

    def encode_packet(self, frame, sequence):
        rgb = self.prepare(frame)
        for z,p in self.corrections:
            rgb[z,p] = rgb[z,p][:,[1,0,2]]
        packed = rgb565_pack(rgb[np.argsort(self.zone_to_unit)]).astype("<u2").tobytes()
        return HEADER.pack(b"LED2", sequence, 65, 6, len(packed), zlib.crc32(packed)) + packed
