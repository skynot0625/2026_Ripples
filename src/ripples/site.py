"""Export confirmed center beacon XY to a reviewed, fixed site configuration.

Read-only HTTP. Does not follow later simulator edits automatically, change RF
calibration, or move existing spatial memory to a new coordinate frame.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.request import urlopen

import numpy as np

from ripples.paths import ROOT
from ripples.realtime import validate_config


def site_config(template, simulator, venue):
    if venue.get("units") != "mm" or venue.get("id") != "m30":
        raise ValueError("expected M30 venue coordinates in millimetres")
    beacons = simulator.get("layout", {}).get("beacons", {})
    if not 3 <= len(beacons) <= 32:
        raise ValueError("at least three confirmed beacons required")
    bounds = venue["bounds"]
    box = [[bounds["x0"]/1000, bounds["x1"]/1000], [bounds["y0"]/1000, bounds["y1"]/1000]]
    anchors = {}
    for key, point in sorted(beacons.items(), key=lambda v: int(v[0])):
        xy = [point["x"]/1000, point["y"]/1000]
        if not np.isfinite(xy).all() or any(not lo <= v <= hi for v, (lo, hi) in zip(xy, box)):
            raise ValueError("beacon outside venue bounds")
        anchors[str(key)] = {"xy_m": xy, "reference_dbm": -59.0}
    config = dict(template, calibrated=False, coordinates_confirmed=True,
                  position_mode="relative_rssi", bounds_m=box, anchors=anchors)
    config["site"] = {"id": "m30", "source": "exhibition-server GET /api/simulator layout.beacons + /api/venue",
                      "confirmed_on": datetime.now(timezone(timedelta(hours=9))).date().isoformat(), "frame": venue.get("frame", {}),
                      "source_units": "mm", "beacons_mm": beacons,
                      "rf_reference_status": "unmeasured; -59 is unused placeholder in relative_rssi mode",
                      "z_status": "not used for XY; zero in center layout is not a surveyed mounting height"}
    return validate_config(config)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--center", default="http://127.0.0.1:8000")
    p.add_argument("--output", type=Path, required=True, help="new path; existing files are not overwritten")
    a = p.parse_args()
    def get(path):
        with urlopen(a.center.rstrip("/")+path, timeout=3) as response:
            return json.load(response)
    template = json.loads((ROOT/"configs/live.example.json").read_text(encoding="utf-8-sig"))
    config = site_config(template, get("/api/simulator"), get("/api/venue"))
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("x", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(a.output)


if __name__ == "__main__":
    main()
