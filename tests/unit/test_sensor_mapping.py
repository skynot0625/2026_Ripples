import gzip
import json

import numpy as np

from ripples.example_clip import ExampleClip
from ripples.hardware.transport import VALID_MASK
from ripples.visualization import RipplesVisual


def stamp(source, features):
    v = np.zeros((32, 32, 24, 3), np.float32)
    RipplesVisual.stamp(v, {"source": source, "xy": [0, 0], "features": features})
    return v


def test_camera_surface_and_ceiling_are_separate_height_bands():
    camera = stamp("hyeol", {**{f"depth_{i}": 2 for i in range(9)},
                             **{f"coverage_{i}": 1 for i in range(9)}})
    cap = stamp("hyeol", {"ceiling_0": 2})
    camera_z = np.where(camera.any(axis=(0, 1, 3)))[0]
    cap_z = np.where(cap.any(axis=(0, 1, 3)))[0]
    assert camera_z.max() < cap_z.min()
    assert np.count_nonzero(camera.any(axis=3)) > 9  # connecting surface, not just nine dots
    assert not stamp("hyeol", {"depth_0": 2, "coverage_0": 0}).any()
    assert not stamp("hyeol", {}).any()


def test_camera_fixed_distance_axis_and_coverage():
    a = stamp("hyeol", {"depth_4": .3, "coverage_4": 1})
    b = stamp("hyeol", {"depth_4": 3, "coverage_4": 1})
    dim = stamp("hyeol", {"depth_4": .3, "coverage_4": .25})
    assert np.where(a.any(axis=(0, 1, 3)))[0].max() < np.where(b.any(axis=(0, 1, 3)))[0].max()
    assert a.max() > b.max()
    np.testing.assert_allclose(dim, a*.5)


def test_microphone_bars_grow_and_zero_is_silent():
    a = stamp("maek", {"mic_level_0": .2})
    b = stamp("maek", {"mic_level_0": .8})
    assert np.where(a.any(axis=(0, 1, 3)))[0].max() < np.where(b.any(axis=(0, 1, 3)))[0].max()
    assert b.max() > a.max()
    assert not stamp("maek", {f"mic_level_{i}": 0 for i in range(3)}).any()
    left_x = np.where(b.any(axis=(1, 2, 3)))[0]
    right_x = np.where(stamp("maek", {"mic_level_2": .8}).any(axis=(1, 2, 3)))[0]
    assert left_x.max() < right_x.min()


def test_clip_preserves_logical_rgb_and_strip_offsets(tmp_path):
    frame = np.zeros((*VALID_MASK.shape, 3), np.uint8)
    frame[VALID_MASK] = np.arange(8460*3).reshape(8460, 3) % 256
    clip = ExampleClip(tmp_path)
    clip.add([frame, frame//2])
    metadata = clip.close([[-.462, 11.149], [.09, 19.851]])
    assert len(metadata["strips"]) == 180
    assert sum(s["count"] for s in metadata["strips"].values()) == 8460
    assert sum(s["physical_visible_count"] for s in metadata["strips"].values()) == 10260
    raw = np.frombuffer(gzip.decompress((tmp_path/"ripples-activate.rgb.gz").read_bytes()), np.uint8).reshape(8460, 3)
    np.testing.assert_array_equal(raw, frame[VALID_MASK])
    spans = sorted((s["offset"], s["offset"]+s["count"]) for s in metadata["strips"].values())
    assert spans[0][0] == 0 and spans[-1][1] == 8460
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    assert json.loads((tmp_path/"ripples-example.json").read_text())["example"] is True
