"""혈 기구 화면 새 기본 10종 (2026-11-20). 이름은 「혈 01 …」~「혈 10 …」. 천장 거리(sensor.up_distance)를 화면마다 한 가지 방식으로 섞는다.
python3 presets.py <server URL>  → 같은 이름이 있으면 그 composition 을 고치고, 없으면 SENSOR 를 복사해 만든다."""
import copy, json, sys, urllib.request
URL = sys.argv[1].rstrip("/")
TOF = {"near": 200, "far": 4000, "confidence": 30, "interval": 200, "line": 1, "slats": False, "vertical": False, "temporal": True,
       "row": 50, "gain": 1.0, "base": 60, "flow": 1.0}
FX_OFF = {"color": {"on": False, "near": "#ffffff", "mid": "#808080", "far": "#000000", "midpoint": 0.5, "invert": False},
          "steps": {"on": False, "levels": 6}, "pixelate": {"on": False, "block": 4},
          "blur": {"on": False, "kind": "glow", "radius": 4.0, "strength": 1.0}, "mirror": {"mode": "none"},
          "trail": {"on": False, "amount": 0.7, "duration": 1.0, "interval": 0.0}}
def fx(**on):
    e = copy.deepcopy(FX_OFF)
    for k, v in on.items(): e[k].update(v)
    return e
UP = "sensor.up_distance"
def bind(lo, hi, out_lo, out_hi, smooth=0.8, invert=False):   # a field linked to the ceiling distance (m)
    return {"bind": {"source": UP, "in_min": lo, "in_max": hi, "out_min": out_lo, "out_max": out_hi, "invert": invert, "smoothing": smooth}}
n = 0
def layer(renderer, name, params=None, *, source="tof.depth", blend="source-over", color="#ffffff", mono=False,
          w=1.0, h=1.0, x=0.0, y=0.0, opacity=1.0, scale=1.0, rotation=0.0, effects=None, visible=True):
    global n; n += 1
    p = dict(TOF) if source == "tof.depth" else {}
    p.update(params or {})
    val = lambda v: v if isinstance(v, dict) else {"value": v}
    return {"id": f"l{n}", "name": name, "source": source, "renderer": renderer, "visible": visible, "blend": blend, "color": color,
            "mono": mono, "w": w, "h": h, "opacity": val(opacity), "x": val(x), "y": val(y), "scale": val(scale),
            "rotation": val(rotation), "params": p, "effects": effects or copy.deepcopy(FX_OFF)}
def comp(name, *layers): return {"name": name, "canvas": {"background": "#000000", "width": 1920, "height": 1280}, "layers": list(layers)}
UPC = dict(min=0.5, max=4.0, smoothing=0.6)   # the ceiling range used below (the gallery ceiling is about 2.2 m above the device)
P = [
 comp("혈 01 숨쉬는 깊이",   # plain depth, its zoom breathing with the ceiling height
      layer("tof_sensor", "깊이 흑백", {"near": 300, "far": 3500},
            scale=bind(1.5, 3.5, 1.08, 0.94), effects=fx(color={"on": True, "near": "#f4f1ea", "mid": "#7d8a99", "far": "#05070a"},
                                                        blur={"on": True, "kind": "glow", "radius": 6.0, "strength": 0.35}))),
 comp("혈 02 등고 지형",     # 45° relief as a landscape, the ceiling as a thin meter on the right
      layer("tof_relief", "높이맵 45°", {"gain": 1.6, "interval": 150, "line": 1}, w=0.94,
            effects=fx(color={"on": True, "near": "#e8fff7", "mid": "#2fb39a", "far": "#06231f"})),
      layer("updist_meter", "천장 높이", {**UPC, "style": "lines", "line_mm": 100, "scale": True, "thickness": 0.6,
            "color_near": "#e8fff7", "color_mid": "#2fb39a", "color_far": "#0c3a33"}, source=UP, w=0.05, x=0.95)),
 comp("혈 03 점구름",        # sparse point cloud that brightens under a low ceiling
      layer("tof_points", "점군", {"density": 3, "size": 2.0, "size_by_distance": True, "alpha_by_distance": True, "near": 250, "far": 4000},
            opacity=bind(1.5, 4.0, 1.0, 0.55), effects=fx(color={"on": True, "near": "#ffffff", "mid": "#8fb8ff", "far": "#1b2a55"},
                                                        blur={"on": True, "kind": "glow", "radius": 8.0, "strength": 0.5}))),
 comp("혈 04 흐르는 단면",   # a cross-section flowing away, over a slow field of the ceiling distance
      layer("updist_field", "천장 흐름 띠", {**UPC, "bands": 10, "speed": 1.2, "angle": 0, "sharpness": 0.3,
            "color_near": "#2a1a4a", "color_mid": "#140d26", "color_far": "#000000"}, source=UP, opacity=0.6),
      layer("tof_side", "단면 흐름", {"row": 50, "base": 70, "flow": 1.0, "direction": 0, "vanish": 100, "inset": False},
            blend="screen", effects=fx(color={"on": True, "near": "#ffffff", "mid": "#b39cff", "far": "#2a1a4a"}))),
 comp("혈 05 거리의 띠",     # depth cut into bands, a strong stepped palette
      layer("tof_bands", "거리 띠", {"bands": 9, "color_near": "#fff3d6", "color_far": "#1a0f05", "edges": True, "near": 300, "far": 4000},
            effects=fx(color={"on": True, "near": "#fff3d6", "mid": "#e0742f", "far": "#1a0f05"}, steps={"on": True, "levels": 9}))),
 comp("혈 06 실루엣과 천장", # what stands within 2.5 m, in front of a ring that swells with the ceiling
      layer("updist_pulse", "천장 파동", {**UPC, "size": 0.9, "rings": 6, "sensitivity": 1.5, "decay": 1.2, "shape": 0.5,
            "color_near": "#7fd3ff", "color_mid": "#1d4f73", "color_far": "#000000"}, source=UP, opacity=0.8),
      layer("tof_silhouette", "가까운 실루엣", {"near_t": 300, "far_t": 2500, "invert": False}, blend="screen", color="#ffffff", mono=True,
            effects=fx(blur={"on": True, "kind": "glow", "radius": 5.0, "strength": 0.4}))),
 comp("혈 07 움직인 자리",   # only what moved, with a long afterglow, over a very dim depth
      layer("tof_sensor", "어두운 깊이", {"near": 300, "far": 4000}, opacity=0.18),
      layer("tof_motion", "움직임", {"sensitivity": 60, "decay": 0.8}, blend="screen", color="#ffd166", mono=True,
            effects=fx(trail={"on": True, "amount": 0.85, "duration": 2.5, "interval": 0.0},
                       blur={"on": True, "kind": "glow", "radius": 6.0, "strength": 0.6}))),
 comp("혈 08 숫자로 본 공간", # the depth as numbers over a coarse pixel field, the ceiling as a trace at the bottom
      layer("tof_sensor", "픽셀 깊이", {"near": 300, "far": 4000}, opacity=0.35, effects=fx(pixelate={"on": True, "block": 14})),
      layer("tof_numbers", "깊이 숫자", {"cell": 14, "unit": "cm", "shade": True, "grid": False}, blend="screen"),
      layer("updist_trace", "천장 기록", {**UPC, "window": 30, "line": 2, "fill": True, "fade": 0.6,
            "color_near": "#ffffff", "color_mid": "#9aa3ad", "color_far": "#3a4048"}, source=UP, h=0.18, y=0.82, opacity=0.8)),
 comp("혈 09 대칭 등고선",   # contour lines mirrored into a symmetric figure, turning slowly with the ceiling
      layer("tof_contour", "등고선", {"interval": 120, "line": 1, "near": 250, "far": 3500}, rotation=bind(1.5, 3.5, -6, 6),
            effects=fx(mirror={"mode": "lr"}, color={"on": True, "near": "#ffffff", "mid": "#ff7aa8", "far": "#2b0a1a"},
                       blur={"on": True, "kind": "glow", "radius": 4.0, "strength": 0.3}))),
 comp("혈 10 깊이와 높이",   # depth (horizontal) and ceiling (vertical) side by side: the two distances at once
      layer("tof_sensor", "앞의 깊이", {"near": 300, "far": 4000}, w=0.78,
            effects=fx(color={"on": True, "near": "#ffffff", "mid": "#8aa0b8", "far": "#0b1018"}, steps={"on": True, "levels": 12})),
      layer("updist_meter", "위의 높이", {**UPC, "style": "lines", "line_mm": 50, "scale": True, "thickness": 0.9,
            "color_near": "#ffffff", "color_mid": "#8aa0b8", "color_far": "#1c2633"}, source=UP, w=0.2, x=0.8)),
]
def call(op):
    req = urllib.request.Request(f"{URL}/api/visual", data=json.dumps({"op": op, "final": True}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read())
state = json.loads(urllib.request.urlopen(f"{URL}/api/visual", timeout=10).read())
byname = {state["compositions"][c]["name"]: c for c in state["order"] if c in state["compositions"]}
for c in P:
    cid = byname.get(c["name"])
    try:
        if not cid:
            s = call({"op": "duplicate", "id": "sensor", "name": c["name"]}); cid = s["created"]
        call({"op": "set", "id": cid, "composition": c}); print("ok", cid, c["name"])
    except urllib.error.HTTPError as e:
        print("ERR", c["name"], e.read().decode()[:300])
