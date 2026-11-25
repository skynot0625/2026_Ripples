import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest

from ripples.example_form import load_events, to_render_event, validate
from ripples.paths import DATA_DIR
from ripples.sensors import packets, depth_features
from ripples.venue_example import Scene

FOLDER = DATA_DIR/"m30_session"


def sample(role):
    return json.loads((FOLDER/f"sample_{role}.json").read_text())


def test_all_events_have_explicit_units_identity_and_no_future_beacons():
    events = load_events(FOLDER/"events.jsonl")
    assert len(events) == 4800
    assert {e["source"] for e in events} == {"hyeol", "maek"}
    assert all(e["example"] and e["position"]["method"] == "simulation_truth" for e in events)
    assert all(e["t_ms"]-e["position"]["beacon_seq"]*1000 == e["position"]["beacon_age_ms"] for e in events)
    assert events[-1]["t_ms"] == 239900
    assert len({e["session_id"] for e in events}) == 1


def test_manifest_checks_every_generated_payload():
    manifest = json.loads((FOLDER/"manifest.json").read_text())
    for name, spec in manifest["files"].items():
        data = (FOLDER/name).read_bytes()
        assert len(data) == spec["bytes"]
        assert hashlib.sha256(data).hexdigest() == spec["sha256"]


@pytest.mark.parametrize("change", ["not_example", "unknown_key", "nan", "bad_depth", "wrong_rms", "bad_time"])
def test_form_rejects_ambiguous_or_invalid_values(change):
    e = sample("maek" if change == "wrong_rms" else "hyeol")
    if change == "not_example": e["example"] = False
    elif change == "unknown_key": e["position"]["unit"] = "mm"
    elif change == "nan": e["position"]["xy_m"][0] = float("nan")
    elif change == "bad_depth": e["measurement"]["coverage"][0] = 1
    elif change == "wrong_rms": e["measurement"]["mics"] = [1, 1, 1]
    elif change == "bad_time": e["t_ms"] = -1
    with pytest.raises(ValueError): validate(e)


def test_duplicate_and_out_of_order_events_rejected(tmp_path):
    e = sample("hyeol"); p = tmp_path/"events.jsonl"
    p.write_text(json.dumps(e)+"\n"+json.dumps(e)+"\n")
    with pytest.raises(ValueError, match="order"):
        load_events(p)


def test_mapping_preserves_axes_and_missing_values():
    s = Scene(json.loads((FOLDER/"venue.json").read_text(encoding="utf-8")))
    e = sample("hyeol"); e["position"]["xy_m"] = s.led_bounds[:, 0].tolist()
    r = to_render_event(e, s.led_bounds)
    assert r["xy"] == [-5, -5] and "depth_0" not in r["features"]
    e["position"]["xy_m"] = s.led_bounds[:, 1].tolist()
    assert to_render_event(e, s.led_bounds)["xy"] == [5, 5]
    r = to_render_event(sample("maek"), s.led_bounds)
    assert {"mic_level_0", "mic_level_1", "mic_level_2"} <= set(r["features"])
    for i,value in enumerate(sample('maek')['measurement'].get('centroid_hz', [])):
        if value is not None:
            assert r['features'][f'centroid_{i}'] == value


def test_route_stays_in_clearance_grid_and_is_closed():
    scene = Scene(json.loads((FOLDER/"venue.json").read_text(encoding="utf-8")))
    np.testing.assert_allclose(scene.route[0], scene.route[-1])
    assert scene.cumulative[-1] > 40
    for t in np.arange(0, 60, .1):
        xy, _ = scene.pose(t)
        x, y = scene.grid(xy)
        assert scene.safe[y, x] and scene.clearance[y, x] >= .7


def test_wire_sample_reduces_to_same_normalized_hyeol():
    records = list(packets(io.BytesIO((FOLDER/"hyeol_sample.bin").read_bytes())))
    assert [h["type"] for h, _ in records] == ["hello", "tof", "lidar"]
    h, payload = records[1]
    features = depth_features(h, payload, rotate=records[0][0]["rotate"])
    m = sample("hyeol")["measurement"]
    for i in range(9):
        assert features.get(f"depth_{i}") == m["depth_m"][i]
        assert features[f"coverage_{i}"] == m["coverage"][i]
    assert records[-1][0]["distance_mm"]/1000 == m["upward_m"]


def test_example_event_encodes_as_current_tower_packet():
    from ripples.hardware.rgb565 import InstallationEncoder
    from ripples.visualization import RipplesVisual
    from ripples.spikes import spike_frame
    from ripples.hardware.transport import HEADER
    scene = Scene(json.loads((FOLDER/"venue.json").read_text(encoding="utf-8")))
    event = to_render_event(sample("hyeol"), scene.led_bounds)
    frame = RipplesVisual("activate").render(spike_frame([]), 0, events=[event], dt=.1)
    packet = InstallationEncoder().encode_packet(frame, 7)
    magic, seq, slots, flags, size, _ = HEADER.unpack(packet[:20])
    assert (magic, seq, slots, flags, size, len(packet)) == (b"LED2", 7, 65, 6, 24960, 24980)


def test_right_room_fills_led_xy_and_lobby_is_excluded():
    from ripples.paths import ROOT
    from ripples.site import site_config
    scene_doc = json.loads((FOLDER/"venue.json").read_text(encoding="utf-8"))
    scene = Scene(scene_doc)
    config = json.loads((ROOT/"configs/live.m30.json").read_text(encoding="utf-8"))
    np.testing.assert_allclose(scene.led_bounds, [[-.462, 11.149], [.09, 19.851]])
    np.testing.assert_allclose(scene.led_bounds, config["bounds_m"])
    # A refreshed beacon export must not silently restore the full lobby viewport.
    refreshed = site_config(config, {"layout": scene_doc["layout"]}, scene_doc)
    assert refreshed["bounds_m"] == config["bounds_m"]
    e = sample("hyeol")
    e["position"]["xy_m"] = [-5, 10]
    with pytest.raises(ValueError):
        to_render_event(e, scene.led_bounds)
    for t in np.arange(0, 60, .1):
        xy, _ = scene.pose(t)
        assert np.all(xy >= scene.led_bounds[:, 0]) and np.all(xy <= scene.led_bounds[:, 1])
    # X span of same physical route expands ~1.89x without changing the route.
    ratio = np.ptp(scene.bounds[0])/np.ptp(scene.led_bounds[0])
    assert 1.88 < ratio < 1.90
