"""Venue geometry for the simulator (runtime, Mac and Windows read the same JSON file).

    venues/<id>/geometry.json   format "ripples-venue/1" (see README "venue geometry")

2.5D model: a floor height and a ceiling height (either may be overridden per zone, the ceiling may be null =
unknown), plus vertical solids, each a footprint (rect or polygon) extruded from z0 to z1 (z1 null = up to the
ceiling). This holds what the official drawings give (walls, columns, openings, glass, steps, ceiling heights) and is
enough for the two sensors and the collision check:

- ToF (mock_tof.MockToF with a venue): rays hit the floor, the ceiling and the solids' side faces.
- 20 m distance sensor: straight up from the device to the ceiling (or the underside of a solid above it).
  An unknown ceiling gives no reading (None), never a guessed one.
- Collision: the device is a circle (DEVICE_RADIUS_MM) that must stay inside the walkable outline and off every
  solid it can hit (z0 below the device's top).

Coordinates: mm, x/y on the plan (y = the simulator's forward axis at heading 0), z up from the venue's floor.
Every solid carries the source reference and a status (measured / approximate / unknown) from the JSON file.

Exhibition layout (2026-11-06, "layout" in geometry.json): pieces that move between shows are not fixed solids but
are built from that spec and a layout state (simulator_store keeps and saves it):
- big_walls: the large movable walls, each hinged at one end next to a column (they only rotate). State = angle from
  the closed position, 0 … max_deg, toward the room (swing +1 = clockwise on the plan, heading-like).
- small_walls: free 2,445 × 2,445 panels on casters (gap below = the caster height), any number up to max_count.
  State = list of {id, x, y, heading} (centre on the plan, heading = the direction the panel faces).
- windows: the hall-side sliding panels on one track of `bays` bays. State = the bay (1 …) of each panel, strictly
  increasing (they slide sideways only and cannot pass each other).
Only the form and ranges are checked (user 2026-11-06: no collision check when moving pieces, they move at once).
Collision, the up sensor and the ToF trace always use the current layout.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

FORMAT = "ripples-venue/1"
ROOT = Path(__file__).resolve().parent / "venues"
VENUES = ("m30",)  # powerplant: not built yet (README "venue geometry")

# Device footprint and height used for the collision check (2026-11-06, user: "모델링 사이즈 기준"): the circle reaches the
# outer edge of the casters of the /venue3d models drawn after the fabrication photos (혈 645 mm, 맥 H base 685 mm →
# one radius for both), height = the taller mast (맥 2.35 m). Estimates from photos, not measured. Change here only.
DEVICE_RADIUS_MM = 690.0
DEVICE_HEIGHT_MM = 2350.0

# kind -> (assumed reflectance, ToF label id). Reflectances are assumptions, not measured.
KINDS = {
    "wall": (0.70, 12),
    "glass": (0.08, 13),          # clear glass: weak return; what is behind it is not modelled
    "curtain": (0.06, 14),        # black-out curtain
    "column": (0.55, 15),
    "movable_wall": (0.80, 16),
    "door": (0.50, 17),
    "step": (0.30, 18),
    "fixture": (0.40, 19),
    # exhibition mock-up layer (venues/m30/mockup.json): shapes for checking space / sensing, not installation values
    "mock_frame": (0.35, 20),      # steel frame post of the central structure
    "mock_plate": (0.50, 21),      # top plate of the central structure
    "mock_led": (0.60, 22),        # hanging LED strip (geometry only, no lighting control)
    "mock_blind": (0.55, 23),      # ceiling-hung blind
}
MOCK_NAME = {"mock_frame": "심(중앙 구조물)", "mock_plate": "심(중앙 구조물)", "mock_led": "심 LED", "mock_blind": "블라인드(목업)"}
HINGE_SLACK = 150.0  # mm at a big wall's hinge end left out of the layout overlap check
LABEL_PERSON = 9  # same id as mock_tof PERSON
LAYER_FORMAT = "ripples-venue-layer/1"
FLOOR_ALBEDO, CEILING_ALBEDO = 0.30, 0.50
LABEL_FLOOR, LABEL_CEILING = 1, 2  # same ids as mock_tof FLOOR / CEILING


def _poly(item: dict, where: str) -> np.ndarray:
    if "rect" in item:
        r = item["rect"]
        if not (isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) for v in r)) or r[0] >= r[2] or r[1] >= r[3]:
            raise ValueError(f"{where}: rect must be [x0, y0, x1, y1] with x0 < x1, y0 < y1")
        return np.array([[r[0], r[1]], [r[2], r[1]], [r[2], r[3]], [r[0], r[3]]], float)
    p = item.get("poly")
    if not (isinstance(p, list) and len(p) >= 3 and all(isinstance(q, list) and len(q) == 2 for q in p)):
        raise ValueError(f"{where}: needs rect or poly (≥ 3 points)")
    a = np.array(p, float)
    area = 0.5 * np.sum(a[:, 0] * np.roll(a[:, 1], -1) - np.roll(a[:, 0], -1) * a[:, 1])
    return a if area > 0 else a[::-1].copy()  # counter-clockwise


def _inside(poly: np.ndarray, x, y):
    """Point(s) in polygon (crossing number); x, y scalars or arrays."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    inside = np.zeros(np.broadcast(x, y).shape, bool)
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if y0 == y1:
            continue
        cross = ((y0 > y) != (y1 > y))
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
        inside ^= cross & (x < xi)
    return inside


def _polys_overlap(a: np.ndarray, b: np.ndarray, tol: float = 1.0) -> bool:
    """Do two convex-or-simple polygons overlap by more than `tol` mm? (edge crossing or one inside the other;
    touching faces do not count)."""
    def shrink(p):
        c = p.mean(axis=0)
        d = p - c
        n = np.linalg.norm(d, axis=1, keepdims=True)
        return c + d * np.maximum(n - tol, 0) / np.maximum(n, 1e-9)
    a, b = shrink(a), shrink(b)
    if _inside(b, a[:, 0], a[:, 1]).any() or _inside(a, b[:, 0], b[:, 1]).any():
        return True
    for i in range(len(a)):
        p1, p2 = a[i], a[(i + 1) % len(a)]
        for j in range(len(b)):
            q1, q2 = b[j], b[(j + 1) % len(b)]
            d1 = np.cross(p2 - p1, q1 - p1); d2 = np.cross(p2 - p1, q2 - p1)
            d3 = np.cross(q2 - q1, p1 - q1); d4 = np.cross(q2 - q1, p2 - q1)
            if d1 * d2 < 0 and d3 * d4 < 0:
                return True
    return False


def _seg_closest(px: float, py: float, a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    ab = b - a
    t = max(0.0, min(1.0, float(np.dot([px - a[0], py - a[1]], ab) / max(np.dot(ab, ab), 1e-9))))
    return float(a[0] + t * ab[0]), float(a[1] + t * ab[1])


def _seg_dist(px: float, py: float, a: np.ndarray, b: np.ndarray) -> float:
    qx, qy = _seg_closest(px, py, a, b)
    return math.hypot(px - qx, py - qy)


# the layout preset marked "default" replaces the geometry's default layout and start poses (2026-11-06). The checks turn
# this off so their fixed test poses keep the geometry defaults; check_presets turns it on.
USE_DEFAULT_PRESET = True


class Venue:
    def __init__(self, data: dict, path: Path | None = None) -> None:
        if data.get("format") != FORMAT:
            raise ValueError(f"format must be {FORMAT}")
        self.path = path
        self.data = data
        self.id = str(data["id"])
        self.name = str(data["name"])
        self.status = str(data.get("status", "partial"))
        self.floor_z = float(data["floor"]["z"])
        cz = data["ceiling"].get("z")
        self.ceiling_z = None if cz is None else float(cz)
        self.zones = []
        for i, z in enumerate(data.get("zones", [])):
            if z.get("kind") not in ("floor", "ceiling"):
                raise ValueError(f"zones[{i}]: kind must be floor or ceiling")
            zz = z.get("z")
            self.zones.append({"kind": z["kind"], "poly": _poly(z, f"zones[{i}]"), "z": None if zz is None else float(zz), "id": z.get("id", f"zone{i}")})
        # extra layers (e.g. the exhibition mock-up) live in their own files next to geometry.json
        self.layers, self.raw_solids, self.people = [], list(data.get("solids", [])), None
        for spec in data.get("layers", []):
            if path is None:
                raise ValueError("layers need the geometry file path")
            layer = json.loads((path.parent / spec["file"]).read_text(encoding="utf-8"))
            if layer.get("format") != LAYER_FORMAT or layer.get("id") != spec["id"]:
                raise ValueError(f"layer {spec['file']}: format must be {LAYER_FORMAT} and id {spec['id']}")
            for s in layer.get("solids", []):
                if s.get("layer") != spec["id"]:
                    raise ValueError(f"layer {spec['file']}: solid {s.get('id')} must have layer {spec['id']}")
            self.raw_solids += layer.get("solids", [])
            if layer.get("people"):
                self.people = layer["people"]
            if layer.get("center"):
                self.mock_center = [float(c) for c in layer["center"]]
            self.layers.append({"id": spec["id"], "file": spec["file"], "status": layer.get("status", ""), "ref": layer.get("ref", ""),
                                "note": layer.get("note", ""), "solids": len(layer.get("solids", []))})
        ids = [s.get("id") for s in self.raw_solids]
        if len(ids) != len(set(ids)):
            raise ValueError("solid ids must be unique across geometry.json and its layers")
        self.fixed = []
        for i, s in enumerate(self.raw_solids):
            if s.get("kind") not in KINDS:
                raise ValueError(f"solids[{i}] ({s.get('id')}): kind must be one of {list(KINDS)}")
            z1 = s.get("z1")
            self.fixed.append({"id": s.get("id", f"solid{i}"), "kind": s["kind"], "poly": _poly(s, f"solids[{i}]"),
                               "z0": float(s.get("z0", self.floor_z)),
                               "z1": None if z1 is None else float(z1), "status": s.get("status", ""), "layer": s.get("layer"), "raw": s})
        self.walkable = _poly({"poly": data["walkable"]}, "walkable")
        self.layout_spec = data.get("layout") or {}
        # 심 (the exhibition mock-up layer) is placed by the layout too (2026-11-06): its solids as drawn in the layer
        # file around mock_center, moved / turned to layout["sim"]
        self.mock_base = [f for f in self.fixed if f["layer"] == "exhibition_mockup"]
        self.fixed = [f for f in self.fixed if f["layer"] != "exhibition_mockup"]
        if not hasattr(self, "mock_center"):
            self.mock_center = [0.0, 0.0]
        self.start = {d: {k: float(v[k]) for k in ("x", "y", "heading")} for d, v in data["start"].items() if not d.startswith("_")}
        xs, ys = self.walkable[:, 0], self.walkable[:, 1]
        self.bounds = {"x0": float(xs.min()), "x1": float(xs.max()), "y0": float(ys.min()), "y1": float(ys.max())}
        self.floors = [self.floor_z] + [z["z"] for z in self.zones if z["kind"] == "floor"]
        ceils = [self.ceiling_z] + [z["z"] for z in self.zones if z["kind"] == "ceiling"]
        self.ceiling_known = all(c is not None for c in ceils)
        self.max_ceiling = max((c for c in ceils if c is not None), default=None)
        # saved layout proposals (user 2026-11-06, "기본시안"): venues/<id>/layout_presets.json, shown as buttons in /venue3d;
        # the one marked "default" is the default layout and gives the 혈 / 맥 start poses ("시안 1을 기본으로")
        pf = path.parent / "layout_presets.json" if path else None
        self.layout_presets = json.loads(pf.read_text(encoding="utf-8")).get("presets", []) if pf and pf.exists() else []
        dp = next((q for q in self.layout_presets if q.get("default")), None) if USE_DEFAULT_PRESET else None
        if dp:
            for d, v in (dp.get("devices") or {}).items():
                if d in self.start:
                    self.start[d] = {k: float(v[k]) for k in ("x", "y", "heading")}
        self.layout = None
        self.set_layout(self.default_layout())
        for device, pose in self.start.items():
            if self.blocked(pose["x"], pose["y"]):
                raise ValueError(f"start.{device} collides with the geometry")

    @classmethod
    def load(cls, venue_id: str, root: Path = ROOT) -> "Venue":
        path = root / venue_id / "geometry.json"
        return cls(json.loads(path.read_text(encoding="utf-8")), path)

    # --- exhibition layout ----------------------------------------------------------------------------------------
    def default_layout(self) -> dict:
        dp = next((q for q in getattr(self, "layout_presets", []) if q.get("default")), None) if USE_DEFAULT_PRESET else None
        if dp:
            return json.loads(json.dumps(dp["layout"]))
        sp = self.layout_spec
        out = {"big_walls": {w["id"]: float(w.get("default_deg", 0)) for w in sp.get("big_walls", [])},
                "small_walls": [dict(w) for w in sp.get("small_walls", {}).get("default", [])],
                "windows": [int(b) for b in sp.get("windows", {}).get("default_bays", [])]}
        out["sim"] = {"x": self.mock_center[0], "y": self.mock_center[1], "heading": 0.0}
        if sp.get("pc"):
            out["pc"] = dict(sp["pc"]["default"])
        return out

    def _layout_solids(self, state: dict) -> list[dict]:
        """The movable pieces as solids for a validated layout state."""
        sp, out = self.layout_spec, []

        def solid(sid, kind, poly, z0, z1, status, ref, check=None):
            raw = {"id": sid, "kind": kind, "poly": [[round(float(x), 1), round(float(y), 1)] for x, y in poly], "z0": z0, "z1": z1,
                   "status": status, "ref": ref, "layer": "layout"}
            out.append({"id": sid, "kind": kind, "poly": _poly(raw, sid), "z0": float(z0), "z1": None if z1 is None else float(z1),
                        "status": status, "layer": "layout", "raw": raw, "check": None if check is None else np.array(check, float)})

        def box(cx, cy, ux, uy, length, width):   # rectangle centred at (cx, cy), long side along (ux, uy)
            vx, vy, hl, hw = -uy, ux, length / 2, width / 2
            return [(cx - ux * hl - vx * hw, cy - uy * hl - vy * hw), (cx + ux * hl - vx * hw, cy + uy * hl - vy * hw),
                    (cx + ux * hl + vx * hw, cy + uy * hl + vy * hw), (cx - ux * hl + vx * hw, cy - uy * hl + vy * hw)]

        for w in sp.get("big_walls", []):
            a = math.radians(w["closed_heading"] + w["swing"] * state["big_walls"][w["id"]])
            ux, uy = math.sin(a), math.cos(a)
            hx, hy = w["hinge"]
            L = w["length"]
            # the overlap check leaves out the hinge end (HINGE_SLACK): the drawing has the hinge touching the wall / column there
            fw = sp.get("big_wall_foot")   # the panel over a caster gap, a foot bar across the free end (not the hinge end) (user 2026-11-06)
            gap, ov = (fw["gap"], fw.get("hinge_overhang", 0)) if fw else (0, 0)   # ov: the panel runs past the hinge axis (2026-11-07)
            solid(w["id"], "movable_wall", box(hx + ux * (L - ov) / 2, hy + uy * (L - ov) / 2, ux, uy, L + ov, w["thickness"]), gap, gap + w["height"], w["status"], w["ref"],
                  check=box(hx + ux * (L + HINGE_SLACK) / 2, hy + uy * (L + HINGE_SLACK) / 2, ux, uy, L - HINGE_SLACK, w["thickness"]))
            if fw:
                d = L - fw["width"] / 2
                solid(f'{w["id"]}-foot', "movable_wall", box(hx + ux * d, hy + uy * d, -uy, ux, fw["length"], fw["width"]), 0, gap, fw["status"], fw["ref"])
        sw = sp.get("small_walls", {})
        for p in state["small_walls"]:
            a = math.radians(p["heading"])
            ux, uy = math.cos(a), -math.sin(a)          # along the panel = right of the facing direction
            fx, fy = math.sin(a), math.cos(a)
            solid(p["id"], "movable_wall", box(p["x"], p["y"], ux, uy, sw["width"], sw["thickness"]), sw["gap"], sw["gap"] + sw["height"], sw["status"], sw["ref"])
            for k, side in enumerate((-1, 1)):   # a foot bar across each end, floor to the caster gap
                ex, ey = p["x"] + ux * side * (sw["width"] / 2 - sw["foot_width"] / 2), p["y"] + uy * side * (sw["width"] / 2 - sw["foot_width"] / 2)
                solid(f'{p["id"]}-foot{k + 1}', "movable_wall", box(ex, ey, fx, fy, sw["foot_length"], sw["foot_width"]), 0, sw["gap"], sw["status"], sw["ref"])
        # 심 moved / turned (heading clockwise like the devices), then its computer box
        sim = state.get("sim") or {"x": self.mock_center[0], "y": self.mock_center[1], "heading": 0.0}
        h = math.radians(sim["heading"])
        c, sn, (cx, cy) = math.cos(h), math.sin(h), self.mock_center
        for m in self.mock_base:
            pts = [(sim["x"] + (x - cx) * c + (y - cy) * sn, sim["y"] - (x - cx) * sn + (y - cy) * c) for x, y in m["poly"]]
            raw = {k: v for k, v in m["raw"].items() if k != "rect"}
            raw["poly"] = [[round(x, 1), round(y, 1)] for x, y in pts]
            out.append({"id": m["id"], "kind": m["kind"], "poly": _poly(raw, m["id"]), "z0": m["z0"], "z1": m["z1"], "status": m["status"],
                        "layer": "exhibition_mockup", "raw": raw})
        if sp.get("pc") and state.get("pc"):
            pc, (w_, d_, hh) = state["pc"], sp["pc"]["size"]
            a = math.radians(pc["heading"])
            solid("sim-pc", "fixture", box(pc["x"], pc["y"], math.cos(a), -math.sin(a), w_, d_), 0, hh, sp["pc"]["status"], sp["pc"]["ref"])
        wn = sp.get("windows", {})
        for pid, bay in zip(wn.get("panels", []), state["windows"]):
            x0, x1 = wn["x"]
            y0 = wn["bay_y0"] + wn["bay_w"] * (bay - 1)
            solid(pid, "glass", [(x0, y0), (x1, y0), (x1, y0 + wn["bay_w"]), (x0, y0 + wn["bay_w"])], 0, wn.get("height"), f'{wn["status"]} · {bay}번 칸', wn["ref"])
        return out

    def check_layout(self, value: object) -> dict:
        """Validate a full layout state; returns the normalised state or raises ValueError (nothing changes)."""
        sp = self.layout_spec
        if not isinstance(value, dict) or set(value) - {"big_walls", "small_walls", "windows", "sim", "pc", "screens", "parking"}:
            raise ValueError("layout must be {big_walls, small_walls, windows, sim, pc, screens, parking?}")
        cur = self.layout or self.default_layout()
        out = {"big_walls": dict(cur["big_walls"]), "small_walls": [dict(p) for p in cur["small_walls"]], "windows": list(cur["windows"]),
               "sim": dict(cur.get("sim") or self.default_layout()["sim"])}
        if sp.get("pc"):
            out["pc"] = dict(cur.get("pc") or sp["pc"]["default"])
        for key in ("sim", "pc"):   # a pose on the plan: {x, y, heading}
            if key in value:
                q = value[key]
                if key == "pc" and not sp.get("pc"):
                    raise ValueError("pc: not in this venue")
                if not isinstance(q, dict) or set(q) != {"x", "y", "heading"} or not all(isinstance(q[k], (int, float)) and not isinstance(q[k], bool) and q[k] == q[k] for k in q):
                    raise ValueError(f'{key}: {{"x", "y", "heading"}} numbers')
                out[key] = {"x": round(float(q["x"]), 1), "y": round(float(q["y"]), 1), "heading": round(float(q["heading"]) % 360, 1)}
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v == v
        if "big_walls" in value:
            bw = value["big_walls"]
            specs = {w["id"]: w for w in sp.get("big_walls", [])}
            if not isinstance(bw, dict) or set(bw) - set(specs):
                raise ValueError(f"big_walls: ids must be from {list(specs)}")
            for k, v in bw.items():
                if not num(v) or not 0 <= v <= specs[k]["max_deg"]:
                    raise ValueError(f"big_walls.{k}: angle 0–{specs[k]['max_deg']}°")
                out["big_walls"][k] = round(float(v), 1)
        if "small_walls" in value:
            lst = value["small_walls"]
            mx = sp.get("small_walls", {}).get("max_count", 0)
            if not isinstance(lst, list) or len(lst) > mx:
                raise ValueError(f"small_walls: a list of at most {mx}")
            seen, norm = set(), []
            for p in lst:
                if not isinstance(p, dict) or set(p) != {"id", "x", "y", "heading"} or not isinstance(p["id"], str) \
                        or not p["id"].startswith("small-") or p["id"] in seen or not all(num(p[k]) for k in ("x", "y", "heading")):
                    raise ValueError('small_walls: each {"id": "small-…", "x", "y", "heading"} (unique ids)')
                seen.add(p["id"])
                norm.append({"id": p["id"], "x": round(float(p["x"]), 1), "y": round(float(p["y"]), 1), "heading": round(float(p["heading"]) % 360, 1)})
            out["small_walls"] = norm
        if "windows" in value:
            wn = sp.get("windows", {})
            n, bays, w = len(wn.get("panels", [])), wn.get("bays", 0), value["windows"]
            if not isinstance(w, list) or len(w) != n or not all(isinstance(b, int) and not isinstance(b, bool) and 1 <= b <= bays for b in w) \
                    or any(w[i] >= w[i + 1] for i in range(n - 1)):
                raise ValueError(f"windows: the bays (1–{bays}) of the {n} panels, increasing")
            out["windows"] = list(w)
        # small screens on the movable walls (user 2026-11-06; size / ratio adjustable, default 10" 4:3): {"id": "screen-…", "wall": small / big wall id,
        # "u": mm along the wall from its middle, "z": mm (centre height), "side": 1 | -1 (which face)}. Display only (no
        # solid: 2 cm thick). A screen whose wall is gone is dropped.
        scr = value.get("screens", cur.get("screens", []))
        if not isinstance(scr, list) or len(scr) > 12:
            raise ValueError("screens: a list of at most 12")
        walls = {q["id"] for q in out["small_walls"]} | set(out["big_walls"])
        norm, seen = [], set()
        for q in scr:
            if not isinstance(q, dict) or not {"id", "wall", "u", "z", "side"} <= set(q) <= {"id", "wall", "u", "z", "side", "inch", "ratio"} \
                    or not isinstance(q["id"], str) or not isinstance(q["wall"], str) or q["id"] in seen or not num(q["u"]) or not num(q["z"]) \
                    or q["side"] not in (1, -1) or ("inch" in q and not (num(q["inch"]) and 5 <= q["inch"] <= 65)) \
                    or ("ratio" in q and q["ratio"] not in ("16:9", "16:10", "4:3", "3:4", "9:16")):
                raise ValueError('screens: each {"id", "wall", "u", "z", "side": 1 | -1, "inch"?: 5–65, "ratio"?: 16:9 | 16:10 | 4:3 | 3:4 | 9:16}')
            seen.add(q["id"])
            if q["wall"] in walls:
                norm.append({"id": q["id"], "wall": q["wall"], "u": round(float(q["u"]), 1), "z": round(min(max(float(q["z"]), 100.0), 2900.0), 1), "side": q["side"],
                             "inch": round(float(q.get("inch", 10)), 1), "ratio": q.get("ratio", "4:3")})
        out["screens"] = norm
        # parking (user 2026-11-07, 시안 2): fixed 혈 / 맥 parking poses {"hyeol": {x, y, heading}, …}; null / absent in the
        # state = the boxes follow their screen walls (spec "parking"); not sent = kept as it is
        pk = value["parking"] if "parking" in value else cur.get("parking")
        if pk is not None:
            if not isinstance(pk, dict) or set(pk) - set(self.start) or not all(
                    isinstance(q, dict) and set(q) == {"x", "y", "heading"} and all(num(q[k]) for k in q) for q in pk.values()):
                raise ValueError('parking: null or {"hyeol" | "maek": {"x", "y", "heading"}}')
            out["parking"] = {d: {"x": round(float(q["x"]), 1), "y": round(float(q["y"]), 1), "heading": round(float(q["heading"]) % 360, 1)} for d, q in pk.items()}
        self._layout_solids(out)   # builds (catches malformed values); no overlap check (user 2026-11-06)
        return out

    def set_layout(self, state: dict) -> None:
        """Apply an already validated layout state (check_layout); rebuilds the collision / ToF data in one step."""
        solids = self.fixed + self._layout_solids(state)
        edges, hfaces = self._edges(solids), self._faces(solids)
        self.solids, self.edges, self.hfaces, self.layout = solids, edges, hfaces, state
        self.layout_version = getattr(self, "layout_version", 0) + 1   # mock_tof re-traces when this changes
        self.start.update(self.parking_starts(state))

    def parking_starts(self, state: dict) -> dict:
        """혈 / 맥 start poses (their parking boxes) from their screen walls (layout spec "parking", user 2026-11-07): at
        (u along the wall, n along its facing) from the wall's centre, heading = the wall's + dh. Devices whose wall is not
        in the layout keep their start."""
        out = {d: dict(q) for d, q in ((state or {}).get("parking") or {}).items()}   # fixed in the layout (시안 2)
        walls = {w["id"]: w for w in (state or {}).get("small_walls", [])}
        for d, c in (self.layout_spec.get("parking") or {}).items():
            w = walls.get(c.get("wall")) if isinstance(c, dict) else None
            if w is None or d in out:
                continue
            a = math.radians(w["heading"])
            ux, uy, fx, fy = math.cos(a), -math.sin(a), math.sin(a), math.cos(a)   # along the panel, its facing (as _layout_solids)
            out[d] = {"x": w["x"] + ux * c["u"] + fx * c["n"], "y": w["y"] + uy * c["u"] + fy * c["n"], "heading": (w["heading"] + c["dh"]) % 360.0}
        return out

    # --- side faces of the solids, for the ToF ----------------------------------------------------------------
    def _edges(self, solids: list[dict]) -> np.ndarray:
        rows = []
        for s in solids:
            albedo, label = KINDS[s["kind"]]
            p = s["poly"]
            for i in range(len(p)):
                a, b = p[i], p[(i + 1) % len(p)]
                e = b - a
                L = math.hypot(*e)
                if L < 1e-6:
                    continue
                # counter-clockwise polygon: outward normal is (e_y, -e_x)
                rows.append((a[0], a[1], b[0], b[1], e[1] / L, -e[0] / L, s["z0"], np.inf if s["z1"] is None else s["z1"], albedo, label))
        return np.array(rows, float).reshape(-1, 10)

    def _faces(self, solids: list[dict]) -> list[dict]:
        """Horizontal faces of solids that do not span floor to ceiling (e.g. a plate's underside), for the ToF.
        Thin solids (< 100 mm across, such as LED strips and blinds) only use their side faces."""
        hfaces = []
        for s in solids:
            p = s["poly"]
            span = min(math.hypot(*(p[(i + 1) % len(p)] - p[i])) for i in range(len(p)))  # thickness (shortest side)
            if span < 100:
                continue
            albedo, label = KINDS[s["kind"]]
            if s["z0"] > min(self.floors) + 1:
                hfaces.append({"poly": p, "z": s["z0"], "below": True, "albedo": albedo, "label": label, "bbox": (*p.min(0), *p.max(0))})
            if s["z1"] is not None and (self.max_ceiling is None or s["z1"] < self.max_ceiling - 1):
                hfaces.append({"poly": p, "z": s["z1"], "below": False, "albedo": albedo, "label": label, "bbox": (*p.min(0), *p.max(0))})
        return hfaces

    # --- queries ----------------------------------------------------------------------------------------------
    def floor_at(self, x: float, y: float) -> float:
        z = self.floor_z
        for zone in self.zones:
            if zone["kind"] == "floor" and _inside(zone["poly"], x, y):
                z = zone["z"]
        return z

    def ceiling_at(self, x: float, y: float) -> float | None:
        z = self.ceiling_z
        for zone in self.zones:
            if zone["kind"] == "ceiling" and _inside(zone["poly"], x, y):
                z = zone["z"]
        return z

    def up_distance(self, x: float, y: float, height: float) -> float | None:
        """Straight-up distance (mm) from a sensor `height` above the floor at (x, y); None if the ceiling is unknown."""
        z = self.floor_at(x, y) + height
        top = self.ceiling_at(x, y)
        for s in self.solids:
            if s["z0"] > z and (top is None or s["z0"] < top) and _inside(s["poly"], x, y):
                top = s["z0"]
        return None if top is None else top - z

    def blocked(self, x: float, y: float, radius: float = DEVICE_RADIUS_MM, overhead: bool = False) -> str | None:
        """Id of what a device circle at (x, y) would hit, "outside" if it leaves the walkable area, else None.
        overhead=True also counts solids above the device height (people keep clear of everything)."""
        floor = self.floor_at(x, y)
        for s in self.solids:  # solids first, so the report names the wall / column that was hit
            if s["z0"] >= floor + DEVICE_HEIGHT_MM and not overhead:
                continue  # overhead, the device passes under it
            p = s["poly"]
            if _inside(p, x, y) or any(_seg_dist(x, y, p[i], p[(i + 1) % len(p)]) < radius for i in range(len(p))):
                return s["id"]
        w = self.walkable
        if not _inside(w, x, y) or any(_seg_dist(x, y, w[i], w[(i + 1) % len(w)]) < radius for i in range(len(w))):
            return "outside"
        return None

    def layout_hit(self, x: float, y: float, radius: float = DEVICE_RADIUS_MM - 1) -> str | None:
        """Id of the movable layout piece a device circle at (x, y) would overlap (fixed solids not counted)."""
        for s in self.solids:
            if s["layer"] != "layout" or s["z0"] >= self.floor_at(x, y) + DEVICE_HEIGHT_MM:
                continue
            p = s["poly"]
            if _inside(p, x, y) or any(_seg_dist(x, y, p[i], p[(i + 1) % len(p)]) < radius for i in range(len(p))):
                return s["id"]
        return None

    def contact_normal(self, x: float, y: float, reach: float = DEVICE_RADIUS_MM + 5.0) -> tuple[float, float] | None:
        """Unit normal pointing from the nearest obstacle edge within `reach` toward the device centre (x, y), or None.
        Used to slide along a wall instead of stopping (simulator_store.SimulatorStore.step)."""
        floor, best = self.floor_at(x, y), None
        edges = []
        for s in self.solids:
            if s["z0"] < floor + DEVICE_HEIGHT_MM:
                p = s["poly"]
                edges += [(p[i], p[(i + 1) % len(p)]) for i in range(len(p))]
        w = self.walkable
        edges += [(w[i], w[(i + 1) % len(w)]) for i in range(len(w))]
        for a, b in edges:
            qx, qy = _seg_closest(x, y, a, b)
            d = math.hypot(x - qx, y - qy)
            if d <= reach and (best is None or d < best[0]):
                best = (d, qx, qy)
        if best is None or best[0] < 1e-6:
            return None
        d, qx, qy = best
        return (x - qx) / d, (y - qy) / d

    def view(self) -> dict:
        """Read-only geometry for the 3D check viewer (/venue3d): the file as written ("source") plus resolved values
        (polygons, and the top of every solid: z1 or, for z1 = null, the ceiling above it; null when that ceiling is
        unknown). Nothing is added or changed; statuses and references are passed through."""
        solids = []
        for s in self.solids:
            p, raw = s["poly"], s["raw"]
            cx, cy = float(p[:, 0].mean()), float(p[:, 1].mean())
            top = s["z1"] if s["z1"] is not None else self.ceiling_at(cx, cy)
            solids.append({"id": s["id"], "kind": s["kind"], "poly": p.round(1).tolist(), "z0": s["z0"], "z1": s["z1"], "top": top,
                           "top_from": "z1" if s["z1"] is not None else ("ceiling" if top is not None else "unknown"),
                           "status": raw.get("status", ""), "ref": raw.get("ref", ""), "layer": raw.get("layer")})
        zones = [{"id": z["id"], "kind": z["kind"], "poly": z["poly"].round(1).tolist(), "z": z["z"]} for z in self.zones]
        return {"id": self.id, "name": self.name, "full_name": self.data.get("full_name", self.name), "status": self.status,
                "status_note": self.data.get("status_note", ""), "units": "mm", "frame": self.data.get("frame", {}),
                "floor": {"z": self.floor_z, "status": self.data["floor"].get("status", "")},
                "ceiling": {"z": self.ceiling_z, "status": self.data["ceiling"].get("status", ""), "detail": self.data["ceiling"].get("detail")},
                "zones": zones, "solids": solids, "walkable": self.walkable.round(1).tolist(), "bounds": self.bounds,
                "kinds": {k: {"albedo": a} for k, (a, _) in KINDS.items()},
                "device": {"radius_mm": DEVICE_RADIUS_MM, "height_mm": DEVICE_HEIGHT_MM},
                "approximations": self.data.get("approximations", []), "unknown": self.data.get("unknown", []),
                "layers": self.layers, "people": self.people, "source": self.data,
                "layout": {"spec": self.layout_spec, "state": self.layout, "mock_center": self.mock_center, "presets": self.layout_presets,
                           "mock_base": [{"id": m["id"], "kind": m["kind"], "poly": m["poly"].round(1).tolist(), "z0": m["z0"], "z1": m["z1"],
                                          "status": m["status"], "ref": m["raw"].get("ref", "")} for m in self.mock_base]}}

    def info(self) -> dict:
        """What the pages need to know (no geometry: the controller shows sensing monitors, not a map)."""
        return {"id": self.id, "name": self.name, "full_name": self.data.get("full_name", self.name), "status": self.status,
                "status_note": self.data.get("status_note", ""), "bounds": self.bounds, "ceiling_known": self.ceiling_known,
                "solids": len(self.solids), "device_radius_mm": DEVICE_RADIUS_MM}

    # --- ToF ray trace ----------------------------------------------------------------------------------------
    def trace(self, origin: tuple[float, float, float], heading_deg: float, xc: np.ndarray, yr: np.ndarray,
              max_mm: float = 8000.0):
        """Trace a level camera (pitch 0) at origin = (x, y, z) facing heading_deg.

        xc (C,) and yr (R,) are the pinhole directions of the sub-ray columns / rows (x right, y up, forward = 1).
        With no pitch every ray in a column has the same horizontal direction, so walls are intersected once per
        column and only heights are checked per pixel. Returns t (distance mm, inf = no hit), label, albedo, |cos|,
        all (R, C). Solids farther than max_mm are skipped (beyond the 4 m range they only ever read as invalid).
        """
        ox, oy, oz = origin
        h = math.radians(heading_deg)
        # horizontal direction per column in plan coordinates: forward (0,1) rotated so heading 90 faces +x
        hx = xc * math.cos(h) + math.sin(h)
        hy = -xc * math.sin(h) + math.cos(h)
        R, C = len(yr), len(xc)
        norm = np.sqrt(xc[None, :] ** 2 + yr[:, None] ** 2 + 1.0)  # |ray| per pixel (unnormalised, forward = 1)
        q = np.full((R, C), np.inf)
        lab = np.zeros((R, C), np.int8)
        alb = np.zeros((R, C))
        cosn = np.zeros((R, C))  # |cos| × |ray| (divided by norm at the end)

        def take(qc, label, albedo, cn):
            m = qc < q
            np.copyto(q, qc, where=m)
            np.copyto(lab, label, where=m, casting="unsafe")
            np.copyto(alb, albedo, where=m)
            np.copyto(cosn, cn, where=m)

        # floor and ceiling (q along the unnormalised ray: z = oz + q·yr)
        floor = self.floor_at(ox, oy)
        with np.errstate(divide="ignore"):
            qf = np.where(yr < 0, (floor - oz) / yr, np.inf)[:, None]
        take(np.broadcast_to(qf, (R, C)), LABEL_FLOOR, FLOOR_ALBEDO, np.broadcast_to(np.abs(yr)[:, None], (R, C)))
        if self.ceiling_z is not None:
            with np.errstate(divide="ignore"):
                qc_ = np.where(yr > 0, (self.ceiling_z - oz) / yr, np.inf)[:, None]
            take(np.broadcast_to(qc_, (R, C)), LABEL_CEILING, CEILING_ALBEDO, np.broadcast_to(np.abs(yr)[:, None], (R, C)))
        for zone in self.zones:
            if zone["z"] is None:
                continue
            want = yr < 0 if zone["kind"] == "floor" else yr > 0
            with np.errstate(divide="ignore", invalid="ignore"):
                qz = np.where(want, (zone["z"] - oz) / yr, np.inf)[:, None] * np.ones((1, C))
            px, py = ox + qz * hx[None, :], oy + qz * hy[None, :]
            qz = np.where(np.isfinite(qz) & _inside(zone["poly"], np.nan_to_num(px), np.nan_to_num(py)), qz, np.inf)
            take(qz, LABEL_FLOOR if zone["kind"] == "floor" else LABEL_CEILING, FLOOR_ALBEDO if zone["kind"] == "floor" else CEILING_ALBEDO,
                 np.broadcast_to(np.abs(yr)[:, None], (R, C)))

        E = self.edges
        if len(E):
            ax, ay, bx, by, nx, ny = E[:, 0], E[:, 1], E[:, 2], E[:, 3], E[:, 4], E[:, 5]
            # facing the camera and within range
            facing = nx * (ox - ax) + ny * (oy - ay) > 0
            ex, ey = bx - ax, by - ay
            L2 = ex * ex + ey * ey
            tproj = np.clip(((ox - ax) * ex + (oy - ay) * ey) / np.maximum(L2, 1e-9), 0, 1)
            near = np.hypot(ox - (ax + tproj * ex), oy - (ay + tproj * ey)) < max_mm
            E = E[facing & near]
        if len(E):
            ax, ay, bx, by, nx, ny, z0, z1, ealb, elab = (E[:, i:i + 1] for i in range(10))
            ex, ey = bx - ax, by - ay
            hxr, hyr = hx[None, :], hy[None, :]
            with np.errstate(divide="ignore", invalid="ignore"):
                denom = hxr * ey - hyr * ex                                   # (E, C)
                qe = ((ax - ox) * ey - (ay - oy) * ex) / denom
                s = ((ax - ox) * hyr - (ay - oy) * hxr) / denom
            qe = np.where((qe > 1e-6) & (s >= 0) & (s <= 1) & np.isfinite(qe), qe, np.inf)
            cn = np.abs(hxr * nx + hyr * ny)                                   # |h · n| (E, C)
            full = ((z0[:, 0] <= min(self.floors) + 1e-6) & (np.isinf(z1[:, 0]) | (self.max_ceiling is not None and z1[:, 0] >= self.max_ceiling)))
            if full.any():
                qf_ = qe[full]
                k = np.argmin(qf_, axis=0)
                cols = np.arange(C)
                qmin = qf_[k, cols]
                take(np.broadcast_to(qmin[None, :], (R, C)), np.broadcast_to(elab[full][k, 0].astype(np.int8)[None, :], (R, C)),
                     np.broadcast_to(ealb[full][k, 0][None, :], (R, C)), np.broadcast_to(cn[full][k, cols][None, :], (R, C)))
            part = np.flatnonzero(~full)
            if len(part):
                # partial-height solids (e.g. hanging LED strips, blinds). On a level camera a vertical face at
                # parameter q covers one contiguous run of rows in its column (z = oz + q·yr, yr falls with the row),
                # so every hit (edge, column) pair becomes a vertical run of cells; keep the nearest per cell.
                ei, ci = np.nonzero(np.isfinite(qe[part]))
                if len(ei):
                    ei = part[ei]
                    qv = qe[ei, ci]
                    with np.errstate(divide="ignore", invalid="ignore"):
                        ylo = (z0[ei, 0] - oz) / qv
                        yhi = np.where(np.isinf(z1[ei, 0]), np.inf, (z1[ei, 0] - oz) / qv)
                    ny = -yr                                              # increasing with the row index
                    rs = np.searchsorted(ny, -yhi, side="left")
                    re = np.searchsorted(ny, -ylo, side="right")
                    L = np.maximum(re - rs, 0)
                    keep = L > 0
                    if keep.any():
                        ei, ci, qv, rs, L = ei[keep], ci[keep], qv[keep], rs[keep], L[keep]
                        total = int(L.sum())
                        rows = np.arange(total) - np.repeat(np.cumsum(L) - L, L) + np.repeat(rs, L)
                        flat = rows * C + np.repeat(ci, L)
                        vals = np.repeat(qv, L)
                        pair = np.repeat(np.arange(len(ei)), L)
                        qf = q.ravel()
                        best = qf.copy()
                        np.minimum.at(best, flat, vals)
                        win = (vals == best[flat]) & (vals < qf[flat])
                        if win.any():
                            f, pw = flat[win], pair[win]
                            e_, c_ = ei[pw], ci[pw]
                            qf[f] = vals[win]
                            lab.ravel()[f] = elab[e_, 0]
                            alb.ravel()[f] = ealb[e_, 0]
                            cosn.ravel()[f] = cn[e_, c_]
        hn = np.hypot(hx, hy)                      # horizontal length of each column's (unnormalised) ray
        hmin, hmax = float(hn.min()), float(hn.max())
        for f in self.hfaces:  # horizontal faces of floating / low solids (e.g. a plate seen from below)
            if (f["below"] and oz >= f["z"]) or (not f["below"] and oz <= f["z"]):
                continue
            # only the rows whose plane hit can fall within the face's distance range (2026-11-06: the tilted 심 plate
            # is 17 slabs; testing every pixel against every slab cost ~20 ms per re-trace)
            x0, y0, x1, y1 = f["bbox"]
            dx_ = max(x0 - ox, 0, ox - x1); dy_ = max(y0 - oy, 0, oy - y1)
            dmin = math.hypot(dx_, dy_)
            dmax = math.hypot(max(abs(x0 - ox), abs(x1 - ox)), max(abs(y0 - oy), abs(y1 - oy)))
            with np.errstate(divide="ignore", invalid="ignore"):
                qr = np.where(yr > 0 if f["below"] else yr < 0, (f["z"] - oz) / yr, np.inf)
            rows = np.flatnonzero(np.isfinite(qr) & (qr * hmax >= dmin) & (qr * hmin <= dmax) & (qr * hmin < max_mm))
            if not len(rows):
                continue
            qz = qr[rows][:, None] * np.ones((1, C))
            px, py = ox + qz * hx[None, :], oy + qz * hy[None, :]
            m = (px >= x0) & (px <= x1) & (py >= y0) & (py <= y1)
            if not m.any():
                continue
            ins = np.zeros_like(m)
            ins[m] = _inside(f["poly"], px[m], py[m])
            full = np.full((R, C), np.inf)
            full[rows] = np.where(ins, qz, np.inf)
            take(full, f["label"], f["albedo"], np.broadcast_to(np.abs(yr)[:, None], (R, C)))

        t = q * norm
        return t, lab, alb, cosn / norm


def load_venue(venue_id: str | None) -> Venue | None:
    """None / "testroom" = the procedural test room (mock_tof.ROOM), kept as fallback and test fixture."""
    if venue_id in (None, "", "testroom"):
        return None
    if venue_id not in VENUES:
        raise ValueError(f"venue must be testroom or one of {list(VENUES)}")
    return Venue.load(venue_id)
