"""Versioned example event form. This is a tower-side form, not a new Pi wire protocol."""
import json
from pathlib import Path

import numpy as np

VERSION = "ripples.example-event/1"


def _keys(obj, required):
    if not isinstance(obj, dict) or set(obj) != set(required):
        raise ValueError(f"expected exactly these fields: {required}")


def _number(value, low, high):
    if type(value) not in (int, float) or not np.isfinite(value) or not low <= value <= high:
        raise ValueError(f"expected finite number in [{low},{high}]")


def validate(event):
    _keys(event, ("schema", "example", "session_id", "source", "seq", "t_ms", "position", "measurement"))
    if event["schema"] != VERSION or event["example"] is not True or event["source"] not in ("hyeol", "maek"):
        raise ValueError("not a supported explicitly marked example event")
    if not isinstance(event["session_id"], str) or not event["session_id"] or len(event["session_id"]) > 80:
        raise ValueError("invalid session id")
    for key in ("seq", "t_ms"):
        if type(event[key]) is not int or not 0 <= event[key] <= 2**53-1:
            raise ValueError(f"invalid {key}")
    p = event["position"]
    _keys(p, ("frame", "method", "xy_m", "beacon_seq", "beacon_age_ms", "relative_xy_m"))
    if p["frame"] != "m30" or p["method"] != "simulation_truth":
        raise ValueError("example uses known simulator position, never claimed measured XY")
    for name in ("xy_m", "relative_xy_m"):
        if not isinstance(p[name], list) or len(p[name]) != 2:
            raise ValueError("position must have two coordinates")
        for v in p[name]:
            _number(v, -100, 100)
    for name in ("beacon_seq", "beacon_age_ms"):
        if type(p[name]) is not int or p[name] < 0:
            raise ValueError("invalid beacon timing")
    if p["beacon_age_ms"] >= 3000 or p["beacon_seq"]*1000+p["beacon_age_ms"] != event["t_ms"]:
        raise ValueError("stale/future beacon: this example uses 1Hz beacons and a shared session clock")
    m = event["measurement"]
    if event["source"] == "hyeol":
        _keys(m, ("depth_m", "coverage", "upward_m"))
        if any(not isinstance(m[k], list) or len(m[k]) != 9 for k in ("depth_m", "coverage")):
            raise ValueError("camera needs nine row-major sectors")
        for d, c in zip(m["depth_m"], m["coverage"]):
            _number(c, 0, 1)
            if d is None:
                if c != 0:
                    raise ValueError("missing depth must have zero coverage")
            else:
                _number(d, 1e-9, 4)
                if c <= 0:
                    raise ValueError("valid depth needs positive coverage")
        if m["upward_m"] is not None:
            _number(m["upward_m"], 1e-9, 59.999999)
    else:
        _keys(m, ("mics", "rms"))
        for name in ("mics", "rms"):
            if not isinstance(m[name], list) or len(m[name]) != 3:
                raise ValueError("three microphones in L/C/R order required")
            for v in m[name]:
                _number(v, 0, 1)
        expected = np.clip((20*np.log10(np.maximum(m["rms"], 1e-7))+70)/50, 0, 1)
        if not np.allclose(m["mics"], expected, atol=.002):
            raise ValueError("mics must follow current Pi -70..-20 dBFS display mapping")
    return event


def to_render_event(event, bounds_m):
    validate(event)
    bounds = np.asarray(bounds_m, float)
    xy = np.asarray(event["position"]["xy_m"], float)
    if bounds.shape != (2, 2) or not np.isfinite(bounds).all() or np.any(bounds[:, 1] <= bounds[:, 0]):
        raise ValueError("invalid bounds")
    if np.any(xy < bounds[:, 0]) or np.any(xy > bounds[:, 1]):
        raise ValueError("position outside this venue")
    m, f = event["measurement"], {}
    if event["source"] == "hyeol":
        for i, (depth, coverage) in enumerate(zip(m["depth_m"], m["coverage"])):
            f[f"coverage_{i}"] = coverage
            if depth is not None:
                f[f"depth_{i}"] = depth
        if m["upward_m"] is not None:
            f["ceiling_0"] = m["upward_m"]
    else:
        f = {f"mic_level_{i}": v for i, v in enumerate(m["mics"])}
    return {"t": event["t_ms"]/1000., "source": event["source"],
            "xy": ((xy-bounds[:, 0])/(bounds[:, 1]-bounds[:, 0])*10-5).tolist(), "features": f}


def load_events(path):
    events, cursors, ids = [], {}, set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        e = validate(json.loads(line))
        key = e["session_id"], e["source"]
        identity = (*key, e["seq"])
        if identity in ids or (key in cursors and (e["seq"] <= cursors[key][0] or e["t_ms"] <= cursors[key][1])):
            raise ValueError("duplicate/out-of-order source event")
        ids.add(identity)
        cursors[key] = e["seq"], e["t_ms"]
        events.append(e)
    return sorted(events, key=lambda e: (e["t_ms"], e["source"]))


def schema():
    """Portable JSON Schema; cross-field time/unit consistency is checked by validate()."""
    number = {"type": "number"}
    def vector(n, item):
        return {"type": "array", "minItems": n, "maxItems": n, "items": item}
    def obj(properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    unit = {"type": "number", "minimum": 0, "maximum": 1}
    positive = {"type": "integer", "minimum": 0, "maximum": 2**53-1}
    shape = obj({"schema": {"const": VERSION}, "example": {"const": True},
                 "session_id": {"type": "string", "minLength": 1, "maxLength": 80},
                 "source": {"enum": ["hyeol", "maek"]}, "seq": positive, "t_ms": positive,
                 "position": obj({"frame": {"const": "m30"}, "method": {"const": "simulation_truth"},
                                  "xy_m": vector(2, number), "relative_xy_m": vector(2, number),
                                  "beacon_seq": positive, "beacon_age_ms": positive}), "measurement": {}})
    hyeol = obj({"depth_m": vector(9, {"type": ["number", "null"], "exclusiveMinimum": 0, "maximum": 4}),
                 "coverage": vector(9, unit), "upward_m": {"type": ["number", "null"], "exclusiveMinimum": 0, "exclusiveMaximum": 60}})
    maek = obj({"mics": vector(3, unit), "rms": vector(3, unit)})
    shape.update({"$schema": "https://json-schema.org/draft/2020-12/schema",
                  "title": "Ripples explicit example event v1",
                  "allOf": [{"if": {"properties": {"source": {"const": "hyeol"}}},
                             "then": {"properties": {"measurement": hyeol}},
                             "else": {"properties": {"measurement": maek}}}]})
    return shape
