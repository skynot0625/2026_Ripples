"""Check the relocated installation assets and the physical RGB565 boundary."""
import importlib
import hashlib
import json
from pathlib import Path
import zlib

import numpy as np

from ripples.hardware.transport import HEADER, VALID_MASK
from ripples.hardware.rgb565 import InstallationEncoder
from ripples.paths import DATA_DIR


def test_all_ripples_modules_import_without_opening_usb():
    source = Path(__file__).resolve().parents[1]/"src"
    for path in (source/"ripples").rglob("*.py"):
        module = ".".join(path.relative_to(source).with_suffix("").parts).removesuffix(".__init__")
        assert Path(importlib.import_module(module).__file__).resolve().is_relative_to(source)


def test_current_installation_packet_and_mask():
    encoder = InstallationEncoder()
    frame = np.full((*VALID_MASK.shape, 3), 40, dtype=np.uint8)
    packet = encoder.encode_packet(frame, 7)
    magic, seq, n, flags, size, checksum = HEADER.unpack(packet[:20])
    assert (magic, seq, n, flags, size) == (b"LED2", 7, 65, 6, 24960)
    assert len(packet) == 24980 and zlib.crc32(packet[20:]) == checksum
    prepared = encoder.prepare(frame)
    assert prepared.shape == (12, 16, 65, 3)
    assert encoder.mask.sum() == 10260 and VALID_MASK.sum() == 8460
    assert not prepared[~encoder.mask].any()
    assert len(encoder.corrections) == 34
    summary = json.loads((DATA_DIR/"layout/installation_summary.json").read_text())
    assert summary["visible_led_count"] == encoder.mask.sum()


def test_supplied_assets_match_manifest_and_firmware_build_record():
    manifest = json.loads((DATA_DIR/"MANIFEST_SHA256.json").read_text(encoding="utf-8"))
    for relative, expected in manifest.items():
        data = (DATA_DIR/relative).read_bytes()
        assert len(data) == expected["bytes"], relative
        assert hashlib.sha256(data).hexdigest() == expected["sha256"], relative
    firmware = DATA_DIR/"firmware_reference"
    build = json.loads((firmware/"lighting_build_plus10.json").read_text())
    assert hashlib.sha256((firmware/"central_usb_rgb565_plus10.bin").read_bytes()).hexdigest() == build["binary_sha256"]
