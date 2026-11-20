"""Server-authoritative visual compositions and the four output slots (시각화 작업실 / 출력 관리).

One file per venue (default: visual.json next to the venue's mapping file, e.g. data/m30/visual.json), kept apart
from mapping.json and simulator.json. Every page reads and writes through the server (/api/visual, /ws/visual).

A composition is an ordered stack of layers (drawn bottom → top) plus canvas settings. Each layer picks an input
source and a renderer (layer type); both are registered in one place, tofrender.js, and used by the 작업실 preview
and every output alike. This store only checks their shape (ids, numbers, ranges), so a new source or renderer needs
no change here. ToF layers (renderer "tof_*") also have their ToF settings checked with the same ranges /config uses.

File schema (version 2):
    {"version": 2, "revision": 9, "updated_at": "...",
     "compositions": {"c1": {"name": "실험 1", "canvas": {"background": "#000000"},
                             "layers": [{"id": "l1", "name": "ToF 등고선", "source": "tof.depth", "renderer": "tof_contour",
                                         "visible": true, "blend": "source-over", "color": "#ffffff", "mono": false,
                                         "w": 1, "h": 1, "opacity": {"value": 1}, "x": {"value": 0}, "y": {"value": 0},
                                         "scale": {"value": 1}, "rotation": {"value": 0},
                                         "params": {"near": 200, "far": 4000, "interval": 100, ...}}]}},
     "order": ["c1"],
     "slots": {"hyeol_device": "sensor", "maek_device": "audio", "hyeol_blind": "sensor", "maek_blind": "audio"}}
- opacity / x / y / scale / rotation: a fixed {"value": v} or an input link {"bind": {"source", "in_min", "in_max",
  "out_min", "out_max", "invert", "smoothing"}} (no formulas). x, y are the layer's offset in canvas widths / heights,
  w, h its size, scale and rotation (degrees) act about the layer's centre.
- ToF renderers: the four modes (tof_sensor / contour / side / relief) and, since 2026-10-15 afternoon, four experiment
  modes (tof_silhouette / tof_bands / tof_points / tof_motion: drawn in the browser from the filtered depth stream).
  Their ToF settings are checked strictly as before; an experiment mode's own settings are clamped into range.
- 깊이 숫자 (tof_numbers, 2026-11-02): one sensor pixel's depth per cell as text, drawn like the experiment modes.
- 평면 점 흔적 (tof_topdown, experiment, 2026-10-30): the ToF points seen from above, piled on a venue map with the
  simulator pose (not a pose estimated from the ToF), plus the up_distance left on the path; settings clamped like 공간선.
- 공간선 (tof_spaceline, experiment, 2026-10-15 night): ToF depth drawn as accumulated 3D lines in the browser; its own
  settings (ART_PARAMS) are clamped like the other experiment modes; one line method at a time ("method").
- Ceiling distance sensor layers (updist_meter / updist_pulse / updist_trace / updist_field, source sensor.up_distance):
  their settings (UPDIST_PARAMS) are clamped into range like the experiment modes; the flow direction is "angle" 0–359°.
  PULSE outline settings (UPDIST_OPTIONAL, 2026-10-15): "shape" / "shape_gain" / "shape_s" = the ring outline from the
  recent distance history (time around the ring; a single-point sensor, not a direction measurement). Kept only when
  given; an older PULSE layer without them is drawn as a plain circle as before (the browser reads a missing "shape"
  as 0). PULSE layers may carry "effects" like ToF layers (the same shared visual experiments).
- SIDE "inset" (optional): false = the flow only (SIDE layers made in the 작업실 from 2026-10-15 evening); missing = the
  ToF picture in the corner as before (built-in SIDE, older files: unchanged). "link" (optional, a layer id): this layer
  is the ToF picture linked to that SIDE layer ("ToF 원본 보기"); at most one per SIDE layer, a link to a layer that is
  not there is cleared. SIDE "direction" (optional, 0–359°, 0 = up, clockwise, missing = 180 = down as before): which way
  the flow moves; the 기준선 ("base", the existing ToF setting) is set with its slider / number field. SIDE "vanish"
  (optional, 5–100 %, missing = 100 = the picture edge as before): 소멸 기준선, where along the flow the snapshots disappear.
- "effects" (optional, ToF layers and PULSE): the per-layer visual experiments (colour mapping, steps, pixelation, blur / glow,
  symmetry, afterimage), all off by default. A layer without the key is drawn as before, so files from before this
  field load and look the same (nothing is rewritten). Values out of range are clamped, unknown ones dropped.
- Built-in compositions (not stored, read-only): SENSOR / CONTOUR / SIDE / RELIEF 45° (one ToF layer each, the server's
  default settings: what the old modes showed) and "audio" (the 맥 audio level diagnostic). They carry the existing
  functions over and serve for checks; they are not proposed artworks. To edit one, duplicate it.
- Slots: 혈 기구 화면 (hyeol_device), 맥 기구 화면 (maek_device), 혈 블라인드 (hyeol_blind), 맥 블라인드 (maek_blind).
  One composition may serve several slots. The space outputs take the blind slot of the role the mapping assignment
  gives them, so the daily 혈 ↔ 맥 rotation moves composition and blinds together.
- Version 1 files (single-mode presets, 2026-10-15 morning) load as one-layer compositions.

Safety (same as mapping_store): write to <file>.tmp, fsync, keep the previous file as <file>.bak, os.replace; a damaged
file at startup is copied aside (<file>.corrupt-<time>) and the .bak is used; invalid requests change nothing.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

TOF_MODES = ("sensor", "contour", "side", "relief")
# ToF renderer → the server stream mode it reads. The experiment modes read the filtered depth of the SIDE stream.
TOF_RENDERERS = {**{f"tof_{m}": m for m in TOF_MODES},
                 "tof_silhouette": "side", "tof_bands": "side", "tof_points": "side", "tof_motion": "side", "tof_numbers": "side",
                 "tof_spaceline": "side", "tof_topdown": "side", "tof_mass": "side"}
# experiment modes' own settings (tofrender.js ART_DEFAULTS / RENDERERS): ("int" | "num", lo, hi, default),
# ("bool", default), ("color", default)
ART_PARAMS = {
    "tof_silhouette": {"near_t": ("int", 0, 4000, 500), "far_t": ("int", 100, 4000, 2500), "invert": ("bool", False)},
    "tof_bands": {"bands": ("int", 2, 24, 6), "color_near": ("color", "#ffffff"), "color_far": ("color", "#333333"), "edges": ("bool", False)},
    "tof_points": {"density": ("int", 1, 8, 2), "size": ("num", 0.5, 6.0, 1.5), "size_by_distance": ("bool", True),
                   "alpha_by_distance": ("bool", True)},
    "tof_motion": {"sensitivity": ("int", 10, 1000, 40), "decay": ("num", 0.0, 3.0, 0.6)},
    # 깎이는 매스 (experiment, 2026-11-02): a block in front of the camera carved by the ToF depth
    "tof_mass": {"method": ("enum", ("press", "space"), "press"), "start_m": ("num", 0.5, 6.0, 2.5), "trace": ("enum", ("regrow", "now"), "regrow"),
                 "front_m": ("num", 0.3, 6.0, 1.0), "depth_m": ("num", 0.1, 5.0, 1.5), "step": ("int", 2, 8, 3), "regrow_s": ("int", 0, 600, 10), "yaw": ("int", -80, 80, 35), "pitch": ("int", -60, 60, 20),
                 "color": ("color", "#d8d0c0"), "color_cut": ("color", "#c0502e")},
    # 깊이 숫자 (2026-11-02): one sensor pixel's depth per cell as text
    "tof_numbers": {"cell": ("int", 4, 30, 10), "unit": ("enum", ("cm", "mm", "m"), "cm"), "shade": ("bool", True), "grid": ("bool", False)},
    # 공간선 (experiment, 3D lines from the ToF depth): one line method, timing, gravity (screen direction), focus / blur
    "tof_spaceline": {"method": ("enum", ("edge", "contour", "mesh", "ir_edge"), "edge"), "edge_mm": ("int", 20, 1000, 150),
                      "interval_mm": ("int", 50, 1000, 250), "step": ("int", 1, 16, 4), "record_s": ("num", 0.05, 2.0, 0.25),
                      "hold_s": ("num", 0.0, 20.0, 2.0), "sag_s": ("num", 0.5, 30.0, 6.0), "fade_s": ("num", 0.5, 30.0, 4.0),
                      "gravity_deg": ("int", 0, 359, 180), "gravity": ("num", 0.0, 3.0, 0.6), "clump": ("num", 0.0, 1.0, 0.4),
                      "focus_x": ("int", 0, 100, 50), "focus_y": ("int", 0, 100, 50), "focus_shift": ("num", -3.0, 3.0, 0.0),
                      "focus_range": ("num", 0.05, 4.0, 0.5), "blur": ("num", 0.0, 16.0, 6.0),
                      "color_near": ("color", "#ffffff"), "color_far": ("color", "#5a6a80"),
                      # 2026-11-10 혈 위치 기준: auto = the simulator with the mock, the ToF estimate on real input (Pi)
                      "pose_src": ("enum", ("auto", "tof", "sim", "camera"), "auto"),
                      "res": ("num", 0.25, 1.0, 1.0),   # 그리기 해상도 (1 = as before)
                      "denoise": ("int", 0, 5, 0)},   # 잡음 거르기 px (0 = as before)
    # 평면 점 흔적 (experiment, 2026-10-30): ToF points seen from above, piled on a venue map with the simulator pose
    "tof_topdown": {"view": ("enum", ("follow", "venue"), "follow"), "range_m": ("num", 2.0, 30.0, 10.0), "step": ("int", 1, 12, 3),
                    "mark_mode": ("enum", ("points", "contour"), "points"), "contour_mm": ("int", 50, 1000, 250),
                    "z_min": ("num", 0.0, 3.0, 0.15), "z_max": ("num", 0.1, 5.0, 2.6), "stamp_s": ("num", 0.05, 2.0, 0.2),
                    "stamp": ("num", 0.02, 1.0, 0.25), "fade_s": ("int", 0, 600, 60), "size": ("num", 0.5, 6.0, 1.5),
                    "color_now": ("color", "#ffffff"), "color_trace": ("color", "#6fa8ff"), "ceil_trace": ("bool", True),
                    "ceil_max": ("num", 1.0, 20.0, 6.0), "ceil_near": ("color", "#ffd27a"), "ceil_far": ("color", "#6a3fa0"),
                    # 2026-10-30: stop stamping without a big movement, the view sector style (ceil_near / ceil_far: no longer drawn)
                    "still_stop": ("bool", True), "still_mm": ("int", 20, 2000, 100), "still_deg": ("int", 1, 90, 5),
                    "fov": ("enum", ("shade", "none"), "shade"),
                    # experiments 3 / 4: time-colour strata, dissolve (defaults = the original look)
                    "trace_style": ("enum", ("add", "age"), "add"), "age_s": ("int", 2, 600, 30), "color_old": ("color", "#5a2d91"),
                    "fade_style": ("enum", ("uniform", "dissolve"), "uniform"), "drift_mm": ("int", 0, 1000, 150),
                    "res_mm": ("int", 10, 40, 10)},   # map pixel = the current points' pixel ("size" is kept but no longer drawn)
}
_UPD_COMMON = {"min": ("num", 0.0, 20.0, 0.0), "max": ("num", 0.5, 20.0, 20.0), "smoothing": ("num", 0.0, 0.95, 0.5),
               "gain": ("num", 0.0, 10.0, 1.0), "invert": ("bool", False), "color_near": ("color", "#ffffff"),
               "color_mid": ("color", "#808080"), "color_far": ("color", "#333333"), "midpoint": ("num", 0.05, 0.95, 0.5)}
# ceiling distance sensor layer types (tofrender.js updistRenderer), own settings + the common ones
UPDIST_PARAMS = {
    "updist_meter": {"thickness": ("num", 0.05, 1.0, 0.3), **_UPD_COMMON},
    "updist_pulse": {"size": ("num", 0.1, 1.5, 0.8), "rings": ("int", 1, 12, 4), "sensitivity": ("num", 0.0, 5.0, 1.0),
                     "decay": ("num", 0.1, 3.0, 0.8), **_UPD_COMMON},
    "updist_trace": {"window": ("int", 5, 120, 20), "line": ("int", 1, 8, 2), "fill": ("bool", False), "fade": ("num", 0.0, 1.0, 0.6), **_UPD_COMMON},
    "updist_field": {"bands": ("int", 2, 40, 8), "speed": ("num", 0.0, 5.0, 1.0), "angle": ("int", 0, 359, 180),
                     "sharpness": ("num", 0.0, 1.0, 0.5), **_UPD_COMMON},
}
# VERTICAL METER scale (2026-11-10): track + ticks + value; stored only when set (older meters = the plain bar, as before)
UPDIST_OPTIONAL = {"updist_meter": {"scale": ("bool", False), "tick": ("num", 0.1, 2.0, 0.5),
                                     # 2026-11-10 선 스타일: stacked thin lines every line_mm instead of a filled bar
                                     "style": ("enum", ("bar", "lines"), "bar"), "line_mm": ("int", 20, 500, 100),
                                     # 2026-11-11 자동 범위: min/max follow a slow average ± auto_span m (0 = off)
                                     "auto_span": ("num", 0.0, 1.5, 0.0), "auto_s": ("int", 2, 120, 20), "line_px": ("int", 0, 60, 0),
                                     "side_fade": ("num", 0.0, 1.0, 0.0), "meter_alpha": ("num", 0.1, 1.0, 1.0)},
                   "updist_pulse": {"shape": ("num", 0.0, 1.0, 0.35), "shape_gain": ("num", 0.1, 20.0, 2.0),
                                     "shape_s": ("num", 1.0, 30.0, 6.0)}}
UPDIST_FX = {"updist_pulse", "updist_meter"}   # number layer types that take the shared visual experiments ("effects")
UPDIST_SOURCE = "sensor.up_distance"
# SIDE settings drawn in the browser only, kept only when given (older SIDE layers have neither: drawn as before):
# inset = the ToF picture in the corner, direction = which way the flow moves (degrees, 0 = up, clockwise; 180 = down)
# vanish = 소멸 기준선, where the flow disappears (% of the way from the baseline to the picture edge; 100 = the edge, as before)
SIDE_OPTIONAL = {"inset": ("bool", True), "direction": ("int", 0, 359, 180), "vanish": ("int", 5, 100, 100)}
# 화소 표현 (2026-11-02) on the layer types drawn per sensor pixel: stored only when set (older layers = "off", as before)
PIXEL_OPTIONAL = {"upscale": ("enum", ("off", "smooth", "contour"), "off")}
SPACELINE_OPTIONAL = {"color_mid": ("color", "#adb5bf"), "fog": ("num", 0.0, 0.9, 0.0), "ir_contour": ("int", 0, 1000, 0), "ghost": ("num", 0.05, 1.0, 1.0), "line_w": ("int", 1, 4, 1), "ir_bold": ("int", 0, 3, 0), "iso_alpha": ("num", 0.1, 1.0, 1.0), "edge_alpha": ("num", 0.1, 1.0, 1.0), "ir_rows": ("int", 0, 20, 0), "auto_color": ("bool", False), "auto_mix": ("num", 0.0, 1.0, 1.0), "auto_span_min": ("num", 0.2, 3.0, 1.0)}   # 공간선 가운데 거리 색 (2026-11-11, 없으면 두 색 사이)
PIXEL_RENDERERS = ("tof_sensor", "tof_silhouette", "tof_bands", "tof_motion")
# visual experiments (tofrender.js EFFECTS), same ranges; ("enum", options, default)
EFFECTS = {
    "color": {"on": ("bool", False), "near": ("color", "#ffffff"), "mid": ("color", "#808080"), "far": ("color", "#000000"),
              "midpoint": ("num", 0.05, 0.95, 0.5), "invert": ("bool", False)},
    "steps": {"on": ("bool", False), "levels": ("int", 2, 32, 6)},
    "pixelate": {"on": ("bool", False), "block": ("int", 2, 24, 4)},
    "blur": {"on": ("bool", False), "kind": ("enum", ("glow", "blur"), "glow"), "radius": ("num", 1.0, 24.0, 4.0), "strength": ("num", 0.1, 2.0, 1.0)},
    "mirror": {"mode": ("enum", ("none", "lr", "tb", "quad"), "none")},
    "trail": {"on": ("bool", False), "amount": ("num", 0.05, 1.0, 0.7), "duration": ("num", 0.1, 5.0, 1.0),
              "interval": ("num", 0.0, 1.0, 0.0)},   # s between kept samples, 0 = every display frame (as before)
}
MODE_NAMES = {"sensor": "SENSOR", "contour": "CONTOUR", "side": "SIDE", "relief": "RELIEF 45°"}
# The server's default ToF settings (server.SharedState).
DEFAULT_PARAMS = {"mode": "sensor", "near": 200, "far": 4000, "confidence": 30, "interval": 200, "line": 1,
                  "slats": False, "vertical": False, "temporal": True, "row": 50, "gain": 1.0, "base": 60, "flow": 1.0}
TOF_KEYS = tuple(k for k in DEFAULT_PARAMS if k != "mode")
BLENDS = ("source-over", "lighter", "screen", "multiply", "difference", "lighten", "darken")
NUM_FIELDS = {"opacity": (0.0, 1.0, 1.0), "x": (-2.0, 2.0, 0.0), "y": (-2.0, 2.0, 0.0), "scale": (0.05, 8.0, 1.0),
              "rotation": (-360.0, 360.0, 0.0)}
SLOTS = ("hyeol_device", "maek_device", "hyeol_blind", "maek_blind")
SLOT_NAMES = {"hyeol_device": "혈 기구 화면", "maek_device": "맥 기구 화면", "hyeol_blind": "혈 블라인드", "maek_blind": "맥 블라인드"}
DEFAULT_SLOTS = {"hyeol_device": "sensor", "maek_device": "audio", "hyeol_blind": "sensor", "maek_blind": "audio"}
MAX_COMPOSITIONS, MAX_LAYERS, MAX_PARAMS = 64, 16, 24
DEBOUNCE_SECONDS = 0.5
CID_RE = re.compile(r"^c[0-9]{1,6}$")
LID_RE = re.compile(r"^l[0-9]{1,4}$")
NAME_RE = re.compile(r"^[a-z0-9_.]{1,48}$")
KEY_RE = re.compile(r"^[a-z_]{1,24}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def default_layer(lid: str = "l1", **over) -> dict:
    layer = {"id": lid, "name": "레이어", "source": None, "renderer": "solid", "visible": True, "blend": "source-over",
             "color": "#ffffff", "mono": False, "w": 1.0, "h": 1.0, "params": {},
             **{k: {"value": d} for k, (_, _, d) in NUM_FIELDS.items()}}
    layer.update(over)
    return layer


def _tof_composition(mode: str) -> dict:
    params = {k: DEFAULT_PARAMS[k] for k in TOF_KEYS}
    return {"name": MODE_NAMES[mode], "canvas": {"background": "#000000"},
            "layers": [default_layer(name=f"ToF {MODE_NAMES[mode]}", source="tof.depth", renderer=f"tof_{mode}", params=params)]}


BUILTINS = {m: _tof_composition(m) for m in TOF_MODES}
BUILTINS["audio"] = {"name": "맥 audio 레벨", "canvas": {"background": "#000000"},
                     "layers": [default_layer(name="audio level", source="audio.level", renderer="meter",
                                              params={"min": 0, "max": 1, "label": "level 0–1"})]}
BUILTINS["maek_wave_sample"] = {"name": "맥 소리 특성 점 격자 시연", "canvas": {"background": "#000000"},
                                "layers": [default_layer(name="합성 음높이와 음량차 방향", source="maek.mic1", renderer="maek_wave",
                                                         params={"grid_px": 22, "speed": 0.32, "life_s": 2.3, "threshold": 0.02,
                                                                 "color": "#ffffff"})]}
BUILTIN_ORDER = (*TOF_MODES, "audio", "maek_wave_sample")


# --- validation --------------------------------------------------------------------------------------------------
def _num(v: object, lo: float, hi: float, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise ValueError(f"{what} must be a number")
    if not lo <= float(v) <= hi:
        raise ValueError(f"{what} must be {lo}–{hi}")
    return round(float(v), 4)


def v_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("name must be a non-empty string")
    name = " ".join(value.split())
    if len(name) > 40:
        raise ValueError("name must be at most 40 characters")
    return name


def v_tof_params(value: object, with_mode: bool = False) -> dict:
    """ToF settings (same ranges as server.SharedState.update_config)."""
    if not isinstance(value, dict):
        raise ValueError("ToF params must be an object")
    allowed = set(DEFAULT_PARAMS) if with_mode else set(TOF_KEYS)
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown ToF params {sorted(unknown)}")
    p = {k: v for k, v in DEFAULT_PARAMS.items() if k in allowed}   # fixed key order (the file is the same on every run)
    if with_mode and "mode" in value:
        if value["mode"] not in TOF_MODES:
            raise ValueError(f"mode must be one of {list(TOF_MODES)}")
        p["mode"] = value["mode"]
    ints = {"near": (0, 3900), "far": (100, 4000), "confidence": (0, 255), "interval": (50, 500), "line": (1, 3),
            "row": (0, 100), "base": (0, 90)}
    for k, (lo, hi) in ints.items():
        if k in value:
            p[k] = int(round(_num(value[k], lo, hi, k)))
    for k, (lo, hi) in {"gain": (0.5, 3.0), "flow": (0.25, 4.0)}.items():
        if k in value:
            p[k] = round(_num(value[k], lo, hi, k), 3)
    for k in ("slats", "vertical", "temporal"):
        if k in value:
            if not isinstance(value[k], bool):
                raise ValueError(f"{k} must be true or false")
            p[k] = value[k]
    p["far"] = max(p["far"], p["near"] + 100)
    return p


def _clamped(spec: tuple, v: object) -> object:
    """One setting forced into its range: numbers clamped, a wrong type / colour / option → the default."""
    kind, default = spec[0], spec[-1]
    if kind in ("int", "num"):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
            return default
        x = min(spec[2], max(spec[1], float(v)))
        return int(round(x)) if kind == "int" else round(x, 3)
    if kind == "bool":
        return v if isinstance(v, bool) else default
    if kind == "color":
        return v.lower() if isinstance(v, str) and COLOR_RE.match(v) else default
    return v if v in spec[1] else default  # enum


def v_effects(value: object) -> dict:
    """The per-layer visual experiments, every group and value present (missing → default, off)."""
    value = value if isinstance(value, dict) else {}
    out = {}
    for group, specs in EFFECTS.items():
        given = value.get(group) if isinstance(value.get(group), dict) else {}
        out[group] = {k: _clamped(spec, given.get(k, spec[-1])) for k, spec in specs.items()}
    return out


def _v_source(v: object) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not NAME_RE.match(v):
        raise ValueError("source must be a source id like 'audio.level' or null")
    return v


def _v_numparam(value: object, field: str) -> dict:
    lo, hi, _ = NUM_FIELDS[field]
    if not isinstance(value, dict) or len(value) != 1 or not ({"value"} >= set(value) or {"bind"} >= set(value)):
        raise ValueError(f"{field} must be {{value}} or {{bind}}")
    if "value" in value:
        return {"value": _num(value["value"], lo, hi, field)}
    b = value["bind"]
    if not isinstance(b, dict) or set(b) - {"source", "in_min", "in_max", "out_min", "out_max", "invert", "smoothing"}:
        raise ValueError(f"{field}.bind: source, in_min, in_max, out_min, out_max, invert, smoothing")
    src = _v_source(b.get("source"))
    if src is None:
        raise ValueError(f"{field}.bind needs a source")
    out = {"source": src, "in_min": _num(b.get("in_min", 0), -1e6, 1e6, "in_min"), "in_max": _num(b.get("in_max", 1), -1e6, 1e6, "in_max"),
           "out_min": _num(b.get("out_min", lo), lo, hi, "out_min"), "out_max": _num(b.get("out_max", hi), lo, hi, "out_max"),
           "invert": bool(b.get("invert", False)), "smoothing": _num(b.get("smoothing", 0), 0, 0.99, "smoothing")}
    if out["in_min"] == out["in_max"]:
        raise ValueError(f"{field}.bind: in_min and in_max must differ")
    return {"bind": out}


def v_layer(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("layer must be an object")
    known = {"id", "name", "source", "renderer", "visible", "blend", "color", "mono", "w", "h", "params", "effects", "link", *NUM_FIELDS}
    if set(value) - known:
        raise ValueError(f"unknown layer fields {sorted(set(value) - known)}")
    lid = value.get("id")
    if not isinstance(lid, str) or not LID_RE.match(lid):
        raise ValueError("layer id must look like 'l1'")
    renderer = value.get("renderer")
    if not isinstance(renderer, str) or not re.match(r"^[a-z0-9_]{1,32}$", renderer):
        raise ValueError("renderer must be a renderer id like 'tof_contour'")
    layer = {"id": lid, "name": v_name(value.get("name", "레이어")), "source": _v_source(value.get("source")), "renderer": renderer,
             "visible": value.get("visible", True), "blend": value.get("blend", "source-over"), "color": value.get("color", "#ffffff"),
             "mono": value.get("mono", False), "w": _num(value.get("w", 1), 0.02, 4, "w"), "h": _num(value.get("h", 1), 0.02, 4, "h")}
    if not isinstance(layer["visible"], bool) or not isinstance(layer["mono"], bool):
        raise ValueError("visible and mono must be true or false")
    if layer["blend"] not in BLENDS:
        raise ValueError(f"blend must be one of {list(BLENDS)}")
    if not isinstance(layer["color"], str) or not COLOR_RE.match(layer["color"]):
        raise ValueError("color must be #rrggbb")
    for f, (_, _, d) in NUM_FIELDS.items():
        layer[f] = _v_numparam(value.get(f, {"value": d}), f)
    params = value.get("params", {})
    if renderer.startswith("tof_"):
        if renderer not in TOF_RENDERERS:
            raise ValueError(f"no ToF renderer {renderer!r}")
        art = ART_PARAMS.get(renderer, {})
        if not isinstance(params, dict):
            raise ValueError("ToF params must be an object")
        optional = SIDE_OPTIONAL if renderer == "tof_side" else PIXEL_OPTIONAL if renderer in PIXEL_RENDERERS else SPACELINE_OPTIONAL if renderer == "tof_spaceline" else {}
        layer["params"] = v_tof_params({k: v for k, v in params.items() if k not in art and k not in optional})
        layer["params"].update({k: _clamped(spec, params.get(k, spec[-1])) for k, spec in art.items()})
        layer["params"].update({k: _clamped(spec, params[k]) for k, spec in optional.items() if k in params})
        if "effects" in value:
            layer["effects"] = v_effects(value["effects"])
    elif renderer in UPDIST_PARAMS:
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        specs, optional = UPDIST_PARAMS[renderer], UPDIST_OPTIONAL.get(renderer, {})
        if set(params) - set(specs) - set(optional):
            raise ValueError(f"unknown settings {sorted(set(params) - set(specs) - set(optional))} for {renderer}")
        layer["params"] = {k: _clamped(spec, params.get(k, spec[-1])) for k, spec in specs.items()}
        layer["params"].update({k: _clamped(spec, params[k]) for k, spec in optional.items() if k in params})
        if renderer in UPDIST_FX and "effects" in value:
            layer["effects"] = v_effects(value["effects"])
        layer["source"] = UPDIST_SOURCE   # these layer types read the ceiling distance sensor only
    else:
        if not isinstance(params, dict) or len(params) > MAX_PARAMS:
            raise ValueError(f"params must be an object with at most {MAX_PARAMS} values")
        clean = {}
        for k, v in params.items():
            if not KEY_RE.match(str(k)):
                raise ValueError(f"bad param name {k!r}")
            if isinstance(v, bool) or (isinstance(v, str) and len(v) <= 40):
                clean[k] = v
            elif isinstance(v, (int, float)) and v == v and abs(v) <= 1e6:
                clean[k] = round(float(v), 4)
            else:
                raise ValueError(f"param {k}: number, true/false or text (≤ 40)")
        layer["params"] = clean
    link = value.get("link")
    if link is not None:
        if not isinstance(link, str) or not LID_RE.match(link):
            raise ValueError("link must be a layer id like 'l1' or null")
        layer["link"] = link
    return layer


def v_composition(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"name", "canvas", "layers"}:
        raise ValueError("composition: name, canvas, layers")
    canvas = value.get("canvas", {"background": "#000000"})
    if not isinstance(canvas, dict) or set(canvas) - {"background", "width", "height"} or not COLOR_RE.match(str(canvas.get("background", "#000000"))):
        raise ValueError("canvas: background #rrggbb, optional width / height")
    # 화면 크기 (2026-11-10): the screen this composition is made for, in pixels; both or neither. Without it the picture
    # fills whatever canvas shows it (as before); with it, it is drawn at that aspect, centred, black outside.
    size = {}
    if "width" in canvas or "height" in canvas:
        w, h = canvas.get("width"), canvas.get("height")
        if not all(isinstance(v, int) and not isinstance(v, bool) and 160 <= v <= 7680 for v in (w, h)):
            raise ValueError("canvas width / height: whole pixels 160–7680, both or neither")
        size = {"width": w, "height": h}
    layers = value.get("layers", [])
    if not isinstance(layers, list) or len(layers) > MAX_LAYERS:
        raise ValueError(f"layers: a list of at most {MAX_LAYERS}")
    out = [v_layer(layer) for layer in layers]
    if len({layer["id"] for layer in out}) != len(out):
        raise ValueError("layer ids must be unique")
    # linked ToF pictures ("ToF 원본 보기"): only to a SIDE layer that is there, one per SIDE layer
    sides = {layer["id"] for layer in out if layer["renderer"] == "tof_side"}
    linked = set()
    for layer in out:
        if "link" not in layer:
            continue
        if layer["link"] not in sides or layer["link"] == layer["id"]:
            del layer["link"]   # its SIDE layer is gone / changed type: an ordinary layer from now on
        elif layer["link"] in linked:
            raise ValueError(f"layer {layer['link']} already has a linked ToF picture")
        else:
            linked.add(layer["link"])
    return {"name": v_name(value.get("name")), "canvas": {"background": canvas.get("background", "#000000").lower(), **size}, "layers": out}


def migrate_v1(data: dict) -> dict:
    """Version 1 (single-mode presets p<n>) → one-layer compositions c<n>; slots follow."""
    comps, order, ids = {}, [], {}
    for pid in data.get("order", []):
        p = data["presets"][pid]
        params = v_tof_params(p.get("params", {}), with_mode=True)
        cid = "c" + pid[1:]
        ids[pid] = cid
        comps[cid] = v_composition({"name": p["name"], "layers": [default_layer(name=f"ToF {MODE_NAMES[params['mode']]}", source="tof.depth",
                                    renderer=f"tof_{params['mode']}", params={k: params[k] for k in TOF_KEYS})]})
        order.append(cid)
    slots = {s: ids.get(v, v) for s, v in (data.get("slots") or {}).items()}
    return {"compositions": comps, "order": order, "slots": slots, "revision": data.get("revision", 0)}


class VisualStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.condition = threading.Condition()
        self.write_lock = threading.Lock()
        self.compositions: dict[str, dict] = {}
        self.order: list[str] = []
        self.slots = dict(DEFAULT_SLOTS)
        self.source = "default"  # default | file | backup | migrated-v1
        self.revision = 0
        self.saved_revision = 0
        self.version = 0
        self.origin: str | None = None
        self.seq: int | None = None
        self.final = True
        self.created: str | None = None  # id made by the last operation (with origin / seq: the page that asked selects it)
        self.save_error: str | None = None
        self._dirty_at: float | None = None
        self._stop = threading.Event()
        self._load()
        threading.Thread(target=self._writer, daemon=True).start()

    # --- load / save -------------------------------------------------------------------------------------------
    def _parse(self, data: object) -> dict:
        if not isinstance(data, dict):
            raise ValueError("top level must be an object")
        migrated = data.get("version") == 1 or ("presets" in data and "compositions" not in data)
        if migrated:
            data = migrate_v1(data)
        raw = data.get("compositions", {})
        if not isinstance(raw, dict):
            raise ValueError("compositions must be an object")
        comps, order = {}, []
        for cid in data.get("order", list(raw)):
            if not CID_RE.match(str(cid)) or cid not in raw:
                raise ValueError(f"bad composition {cid!r}")
            comps[cid] = v_composition(raw[cid])
            order.append(cid)
        slots = dict(DEFAULT_SLOTS)
        for slot, cid in (data.get("slots") or {}).items():
            if slot not in SLOTS or (cid not in comps and cid not in BUILTINS):
                raise ValueError(f"bad slot {slot!r} → {cid!r}")
            slots[slot] = cid
        return {"compositions": comps, "order": order, "slots": slots, "revision": max(0, int(data.get("revision", 0))), "migrated": migrated}

    def _read(self, path: Path) -> dict | None:
        try:
            return self._parse(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            print(f"Visual compositions file ignored ({path.name}): {exc}", flush=True)
            if path == self.path:
                aside = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
                try:
                    shutil.copy2(path, aside)
                    print(f"Damaged visual file kept as {aside.name}", flush=True)
                except OSError:
                    pass
            return None

    def _load(self) -> None:
        for path, source in ((self.path, "file"), (self.path.with_name(self.path.name + ".bak"), "backup")):
            data = self._read(path)
            if data:
                self.compositions, self.order, self.slots, self.revision = data["compositions"], data["order"], data["slots"], data["revision"]
                self.source = "migrated-v1" if data["migrated"] else source
                self.saved_revision = self.revision
                print(f"Visual compositions loaded from {path.name} (revision {self.revision})", flush=True)
                return
        print("Visual compositions: no saved file, using the built-in compositions", flush=True)

    def _write(self) -> None:
        with self.write_lock:
            with self.condition:
                revision = self.revision
                data = {"version": 2, "revision": revision, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "compositions": copy.deepcopy(self.compositions), "order": list(self.order), "slots": dict(self.slots)}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                if self.path.exists():
                    shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
                for attempt in range(5):  # Windows can briefly lock the target (antivirus, indexer)
                    try:
                        os.replace(tmp, self.path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.05)
                error = None
            except OSError as exc:
                error = str(exc)
                print(f"Visual compositions save failed: {exc}", flush=True)
            with self.condition:
                if error is None:
                    self.saved_revision = max(self.saved_revision, revision)
                    self.source = "file"
                self.save_error = error
                self.version += 1
                self.condition.notify_all()

    def _writer(self) -> None:
        while not self._stop.is_set():
            with self.condition:
                self.condition.wait_for(lambda: self._dirty_at is not None or self._stop.is_set(), timeout=1.0)
                due = self._dirty_at
            if due is None:
                continue
            delay = due + DEBOUNCE_SECONDS - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
                continue
            with self.condition:
                self._dirty_at = None
            self._write()

    def stop(self) -> None:
        with self.condition:
            pending = self.revision != self.saved_revision
            self._dirty_at = None
        if pending:
            self._write()
        self._stop.set()
        with self.condition:
            self.condition.notify_all()

    # --- API ---------------------------------------------------------------------------------------------------
    def _all(self) -> dict:
        out = {cid: dict(copy.deepcopy(BUILTINS[cid]), id=cid, builtin=True) for cid in BUILTIN_ORDER}
        out.update({cid: dict(copy.deepcopy(self.compositions[cid]), id=cid, builtin=False) for cid in self.order})
        return out

    def composition(self, cid: str) -> dict | None:
        with self.condition:
            if cid in BUILTINS:
                return dict(copy.deepcopy(BUILTINS[cid]), id=cid, builtin=True)
            if cid in self.compositions:
                return dict(copy.deepcopy(self.compositions[cid]), id=cid, builtin=False)
            return None

    def snapshot(self) -> dict:
        with self.condition:
            return {"type": "state", "version": self.version, "revision": self.revision,
                    "compositions": self._all(), "order": [*BUILTIN_ORDER, *self.order],
                    "slots": dict(self.slots), "slot_names": dict(SLOT_NAMES), "blends": list(BLENDS),
                    "source": self.source, "origin": self.origin, "seq": self.seq, "final": self.final, "created": self.created,
                    "saved": self.saved_revision == self.revision, "save_error": self.save_error, "path": str(self.path.name)}

    def _apply(self, comps: dict, order: list, slots: dict, op: dict) -> str | None:
        kind = op.get("op")
        if kind == "assign":
            slot, cid = op.get("slot"), op.get("id")
            if slot not in SLOTS:
                raise ValueError(f"slot must be one of {list(SLOTS)}")
            if cid not in comps and cid not in BUILTINS:
                raise ValueError(f"no composition {cid!r}")
            slots[slot] = cid
            return None
        if kind == "duplicate":
            if len(order) >= MAX_COMPOSITIONS:
                raise ValueError(f"at most {MAX_COMPOSITIONS} saved compositions")
            src = op.get("id")
            base = comps.get(src) or BUILTINS.get(src)
            if base is None:
                raise ValueError(f"no composition {src!r} to duplicate")
            name = op.get("name") or f"{base['name']} 복사본"
            new = v_composition(dict(copy.deepcopy(base), name=name))
            n = 1
            while f"c{n}" in comps:
                n += 1
            cid = f"c{n}"
            comps[cid] = new
            order.append(cid)
            return cid
        if kind in ("rename", "set"):
            cid = op.get("id")
            if cid in BUILTINS:
                raise ValueError("built-in compositions cannot be changed (duplicate it first)")
            if cid not in comps:
                raise ValueError(f"no composition {cid!r}")
            if kind == "rename":
                comps[cid]["name"] = v_name(op.get("name"))
            else:
                comps[cid] = v_composition(op.get("composition"))
            return None
        raise ValueError("op must be duplicate, rename, set or assign")

    def apply(self, op: object, final: bool, origin: str | None = None, seq: int | None = None) -> dict:
        """Apply one operation. Raises ValueError without changing anything if the request is invalid."""
        if not isinstance(op, dict):
            raise ValueError("operation must be an object")
        with self.condition:
            comps, order, slots = copy.deepcopy(self.compositions), list(self.order), dict(self.slots)
        self._apply(comps, order, slots, op)
        with self.condition:
            # re-apply on the current state so a concurrent change from another page is not lost
            comps, order, slots = copy.deepcopy(self.compositions), list(self.order), dict(self.slots)
            created = self._apply(comps, order, slots, op)
            self.compositions, self.order, self.slots = comps, order, slots
            self.revision += 1
            self.version += 1
            self.origin, self.seq, self.final, self.created = origin, seq, final, created
            self._dirty_at = None if final else (self._dirty_at or time.monotonic())
            self.condition.notify_all()
        if final:
            self._write()
        return self.snapshot()

    def replace(self, data: object, origin: str = "pi-follow") -> dict:
        """The whole file at once (Pi 실행본 following 「Pi로 보내기」: pi_bundle.follow). Raises ValueError without
        changing anything if the data is invalid; otherwise the pages see it at once and it is saved."""
        parsed = self._parse(data)
        with self.condition:
            self.compositions, self.order, self.slots = parsed["compositions"], parsed["order"], parsed["slots"]
            self.revision = parsed["revision"]
            self.version += 1
            self.origin, self.seq, self.final, self.created = origin, None, True, None
            self._dirty_at = None
            self.condition.notify_all()
        self._write()
        return self.snapshot()

    def wait_change(self, last_version: int, timeout: float) -> int:
        with self.condition:
            self.condition.wait_for(lambda: self.version != last_version or self._stop.is_set(), timeout=timeout)
            return self.version
