#!/usr/bin/env python3
"""Checks for the visual compositions / output slots store (visual_store.py), no server or browser needed.

    python visual_check.py                     (store checks)
    python visual_check.py data/m30/visual.json   (also: that saved file loads and saves back unchanged)

Prints one PASS / FAIL line per check, exits 1 on any failure.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
from pathlib import Path

from visual_store import ART_PARAMS, BUILTINS, UPDIST_PARAMS, DEFAULT_PARAMS, DEFAULT_SLOTS, EFFECTS, MAX_COMPOSITIONS, MAX_LAYERS, SLOTS, TOF_KEYS, VisualStore, default_layer, v_effects

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))


def rejected(store: VisualStore, op: object) -> bool:
    try:
        store.apply(op, final=True)
    except ValueError:
        return True
    return False


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "m30" / "visual.json"
        s = VisualStore(path)
        snap = s.snapshot()
        tof_ok = all(snap["compositions"][m]["layers"][0]["renderer"] == f"tof_{m}" and snap["compositions"][m]["layers"][0]["params"] == {k: DEFAULT_PARAMS[k] for k in TOF_KEYS}
                     for m in ("sensor", "contour", "side", "relief"))
        check(not path.exists() and snap["slots"] == DEFAULT_SLOTS and snap["order"][:5] == ["sensor", "contour", "side", "relief", "audio"] and tof_ok
              and snap["compositions"]["audio"]["layers"][0]["source"] == "audio.level",
              "no file: built-in compositions = one ToF layer with the server defaults per mode + the audio meter; slots as before; nothing written")

        cid = s.apply({"op": "duplicate", "id": "contour"}, final=True, origin="t", seq=1)["created"]
        comp = copy.deepcopy(s.composition(cid))
        comp["layers"].append(default_layer("l2", name="audio", source="audio.level", renderer="meter", blend="screen", params={"min": 0, "max": 1, "label": "x"}))
        comp["layers"].append(default_layer("l3", name="면", renderer="solid", w=0.25, h=0.25, color="#ff8800",
                                            opacity={"bind": {"source": "sensor.up_distance", "in_min": 0, "in_max": 3, "out_min": 0.1, "out_max": 1, "invert": True, "smoothing": 0.5}}))
        comp["layers"][0]["params"]["interval"] = 100
        r = s.apply({"op": "set", "id": cid, "composition": {"name": "겹침 확인", "canvas": comp["canvas"], "layers": comp["layers"]}}, final=True, origin="t", seq=2)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        saved = on_disk["compositions"][cid]
        check(r["created"] is None and cid == "c1" and on_disk["version"] == 2 and saved["name"] == "겹침 확인" and [l["renderer"] for l in saved["layers"]] == ["tof_contour", "meter", "solid"]
              and saved["layers"][0]["params"]["interval"] == 100 and saved["layers"][2]["opacity"]["bind"]["invert"] is True,
              "duplicate a built-in, then save the whole composition (3 layers, ToF setting, blend, input link) at once", f"{cid}, revision {on_disk['revision']}")
        s.apply({"op": "assign", "slot": "hyeol_blind", "id": cid}, final=True)
        s.apply({"op": "assign", "slot": "hyeol_device", "id": cid}, final=True)
        s.apply({"op": "rename", "id": cid, "name": "  겹침   확인 2 "}, final=True)
        check(s.snapshot()["slots"]["hyeol_blind"] == cid and s.snapshot()["slots"]["hyeol_device"] == cid and s.snapshot()["compositions"][cid]["name"] == "겹침 확인 2",
              "one composition assigned to two slots, rename (spaces tidied)")

        before = path.read_bytes()
        good = copy.deepcopy(s.composition(cid))
        layer = good["layers"][0]

        def with_layer(**over):
            c = {"name": "x", "canvas": {"background": "#000000"}, "layers": [dict(copy.deepcopy(layer), **over)]}
            return {"op": "set", "id": cid, "composition": c}
        bad = [{"op": "set", "id": "contour", "composition": {"name": "x", "layers": []}}, {"op": "rename", "id": "sensor", "name": "x"},
               {"op": "assign", "slot": "hyeol_blind", "id": "nope"}, {"op": "assign", "slot": "booth_a", "id": "sensor"},
               with_layer(renderer="Bad Renderer!"), with_layer(renderer="tof_rainbow"), with_layer(blend="hue"), with_layer(color="red"),
               with_layer(params={"far": 9000}), with_layer(params={"interval": 100, "unknown": 1}), with_layer(opacity={"value": 2}),
               with_layer(opacity={"bind": {"source": "audio.level", "in_min": 1, "in_max": 1}}), with_layer(opacity={"bind": {"in_min": 0, "in_max": 1}}),
               with_layer(x={"value": 0, "bind": {}}), with_layer(source="../etc"), with_layer(id="layer one"),
               {"op": "set", "id": cid, "composition": {"name": "x", "layers": [layer, layer]}},
               {"op": "set", "id": cid, "composition": {"name": "x", "layers": [dict(copy.deepcopy(layer), id=f"l{i}") for i in range(MAX_LAYERS + 1)]}},
               {"op": "set", "id": cid, "composition": {"name": "x", "canvas": {"background": "black"}, "layers": []}},
               {"op": "duplicate", "id": "nope"}, {"op": "delete", "id": cid}, {"op": "rename", "id": cid, "name": "x" * 41}, "not an object"]
        check(all(rejected(s, op) for op in bad) and path.read_bytes() == before,
              "invalid requests rejected, file unchanged (built-ins read-only, bad renderer / ToF setting / blend / colour / link / ids, layer limit, no delete)", f"{len(bad)} requests")
        generic = with_layer(renderer="future_layer_type", source="future.input", params={"count": 3, "on": True, "label": "abc"})
        s.apply(generic, final=True)
        check(s.composition(cid)["layers"][0]["renderer"] == "future_layer_type",
              "a renderer / source id the store does not know is accepted by shape (new layer types need no server change)")
        s.apply({"op": "set", "id": cid, "composition": {"name": "겹침 확인 2", "canvas": comp["canvas"], "layers": comp["layers"]}}, final=True)

        # restart
        s.stop()
        s2 = VisualStore(path)
        c2 = s2.composition(cid)
        check(s2.snapshot()["source"] == "file" and s2.snapshot()["slots"]["hyeol_blind"] == cid and [l["renderer"] for l in c2["layers"]] == ["tof_contour", "meter", "solid"]
              and c2["layers"][2]["opacity"]["bind"]["source"] == "sensor.up_distance", "restart: compositions (layers, order, links) and slots restored")
        # damaged file: copied aside, the .bak is used
        s2.apply({"op": "rename", "id": cid, "name": "최신 이름"}, final=True)
        s2.stop()
        path.write_text("{ broken", encoding="utf-8")
        s3 = VisualStore(path)
        corrupt = list(path.parent.glob("visual.json.corrupt-*"))
        check(s3.snapshot()["source"] == "backup" and corrupt and s3.composition(cid)["name"] == "겹침 확인 2",
              "damaged file: kept aside, the previous good file (.bak) is used", f"{[c.name for c in corrupt]}")
        s3.stop()

        # version 1 file (single-mode presets, 2026-10-15 morning) loads as one-layer compositions
        p1 = Path(d) / "v1" / "visual.json"
        p1.parent.mkdir()
        p1.write_text(json.dumps({"version": 1, "revision": 4, "presets": {"p1": {"name": "등고선 100", "kind": "tof", "params": dict(DEFAULT_PARAMS, mode="contour", interval=100)}},
                                  "order": ["p1"], "slots": {"hyeol_blind": "p1", "maek_blind": "audio"}}), encoding="utf-8")
        s5 = VisualStore(p1)
        c5 = s5.composition("c1")
        check(s5.snapshot()["source"] == "migrated-v1" and c5 and c5["layers"][0]["renderer"] == "tof_contour" and c5["layers"][0]["params"]["interval"] == 100
              and s5.snapshot()["slots"]["hyeol_blind"] == "c1", "version 1 presets load as one-layer compositions, slots follow")
        s5.stop()

        # limit, concurrency
        p2 = Path(d) / "limit" / "visual.json"
        s4 = VisualStore(p2)
        for _ in range(MAX_COMPOSITIONS):
            s4.apply({"op": "duplicate", "id": "sensor"}, final=False)
        check(rejected(s4, {"op": "duplicate", "id": "sensor"}), f"at most {MAX_COMPOSITIONS} saved compositions")
        errors = []

        def assign(slot: str, cid_: str) -> None:
            try:
                for _ in range(20):
                    s4.apply({"op": "assign", "slot": slot, "id": cid_}, final=False)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        ts = [threading.Thread(target=assign, args=(slot, f"c{i + 1}")) for i, slot in enumerate(SLOTS)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        s4.stop()
        final = json.loads(p2.read_text(encoding="utf-8"))
        check(not errors and final["slots"] == {slot: f"c{i + 1}" for i, slot in enumerate(SLOTS)},
              "four pages assigning different slots at once: no change lost, pending changes flushed on stop", str(final["slots"]))
        check(set(BUILTINS) == {"sensor", "contour", "side", "relief", "audio", "maek_wave_sample"}
              and BUILTINS["maek_wave_sample"]["layers"][0]["renderer"] == "maek_wave",
              "built-ins: four ToF modes, original audio meter, and separate 맥 wave sample")

        # --- experiment modes and visual experiments (2026-10-15 afternoon) ---
        p3 = Path(d) / "fx" / "visual.json"
        s6 = VisualStore(p3)
        cid6 = s6.apply({"op": "duplicate", "id": "contour"}, final=True)["created"]
        tof = {k: DEFAULT_PARAMS[k] for k in TOF_KEYS}
        layers = [default_layer("l1", name="실루엣", source="tof.depth", renderer="tof_silhouette", params=dict(tof, near_t=99999, far_t=-5, invert="yes")),
                  default_layer("l2", name="띠", source="tof.depth", renderer="tof_bands", blend="screen", params=dict(tof, bands=1, color_near="red", edges=True)),
                  default_layer("l3", name="점", source="tof.depth", renderer="tof_points", params={"density": 3}),
                  default_layer("l4", name="움직임", source="tof.depth", renderer="tof_motion", params=dict(tof, sensitivity=5.5, decay=9),
                                effects={"trail": {"on": True, "duration": 100, "amount": "lots"}, "blur": {"on": True, "kind": "x", "radius": 0},
                                         "color": {"on": True, "near": "#FF0000", "far": "black", "midpoint": 2}, "steps": {"on": True, "levels": 1},
                                         "mirror": {"mode": "quad"}, "pixelate": "big", "glitter": {"on": True}})]
        s6.apply({"op": "set", "id": cid6, "composition": {"name": "실험 모드", "layers": layers}}, final=True)
        c6 = s6.composition(cid6)
        L = {l["id"]: l for l in c6["layers"]}
        fx = L["l4"]["effects"]
        check(L["l1"]["params"]["near_t"] == 4000 and L["l1"]["params"]["far_t"] == 100 and L["l1"]["params"]["invert"] is False
              and L["l2"]["params"]["bands"] == 2 and L["l2"]["params"]["color_near"] == "#ffffff" and L["l2"]["params"]["edges"] is True
              and L["l3"]["params"] == dict(tof, density=3, size=1.5, size_by_distance=True, alpha_by_distance=True)
              and L["l4"]["params"]["sensitivity"] == 10 and L["l4"]["params"]["decay"] == 3.0,
              "experiment modes (silhouette / bands / points / motion) accepted; their own settings clamped, missing ones = defaults")
        check(fx["trail"] == {"on": True, "amount": 0.7, "duration": 5.0, "interval": 0.0} and fx["blur"] == {"on": True, "kind": "glow", "radius": 1.0, "strength": 1.0}
              and fx["color"] == {"on": True, "near": "#ff0000", "mid": "#808080", "far": "#000000", "midpoint": 0.95, "invert": False}
              and fx["steps"] == {"on": True, "levels": 2} and fx["mirror"] == {"mode": "quad"} and fx["pixelate"] == {"on": False, "block": 4}
              and set(fx) == set(EFFECTS) and all("effects" not in L[k] for k in ("l1", "l2", "l3")),
              "visual experiments: out-of-range values clamped, wrong types → default, unknown groups dropped; layers without them stay without")
        check(v_effects({}) == {g: {k: spec[-1] for k, spec in specs.items()} for g, specs in EFFECTS.items()}
              and not any(g.get("on") for g in v_effects({}).values()) and v_effects({})["mirror"]["mode"] == "none",
              "visual experiments default: every one off")
        bad6 = [{"op": "set", "id": cid6, "composition": {"name": "x", "layers": [default_layer("l1", renderer="tof_glitch", params=tof)]}},
                {"op": "set", "id": cid6, "composition": {"name": "x", "layers": [default_layer("l1", renderer="tof_points", params={"far": 9000})]}},
                {"op": "set", "id": cid6, "composition": {"name": "x", "layers": [default_layer("l1", renderer="tof_points", params={"sparkle": 1})]}}]
        before6 = p3.read_bytes()
        check(all(rejected(s6, op) for op in bad6) and p3.read_bytes() == before6,
              "still rejected: an unknown ToF renderer, an out-of-range ToF setting (far 9000), an unknown setting name")
        s6.stop()
        s7 = VisualStore(p3)
        check(s7.composition(cid6) == c6, "restart: experiment modes and visual experiments restored exactly")
        s7.stop()

        # --- 공간선 (experiment): settings clamped, one method only, defaults filled
        sl = default_layer("l1", name="공간선", source="tof.depth", renderer="tof_spaceline", params=dict(tof, method="all", gravity_deg=720, blur=99, hold_s=-1, color_far="blue"))
        s6b = VisualStore(p3)
        s6b.apply({"op": "set", "id": cid6, "composition": {"name": "공간선", "layers": [sl]}}, final=True)
        sp = s6b.composition(cid6)["layers"][0]["params"]
        check(sp["method"] == "edge" and sp["gravity_deg"] == 359 and sp["blur"] == 16.0 and sp["hold_s"] == 0.0 and sp["color_far"] == "#5a6a80"
              and set(sp) == set(tof) | set(ART_PARAMS["tof_spaceline"]),
              "공간선: one line method (an unknown one → 경계 윤곽), settings clamped, all present")
        td = default_layer("l1", name="평면", source="tof.depth", renderer="tof_topdown", params=dict(tof, view="map", fade_s=9999, step=0, ceil_trace="yes", color_trace="red"))
        s6b.apply({"op": "set", "id": cid6, "composition": {"name": "평면", "layers": [td]}}, final=True)
        tp = s6b.composition(cid6)["layers"][0]["params"]
        check(tp["view"] == "follow" and tp["fade_s"] == 600 and tp["step"] == 1 and tp["ceil_trace"] is True and tp["color_trace"] == "#6fa8ff"
              and set(tp) == set(tof) | set(ART_PARAMS["tof_topdown"]),
              "평면 점 흔적: view follow / venue only, settings clamped (lifetime ≤ 600 s), all present")
        s6b.stop()

        # --- ceiling distance sensor layers, SIDE flow only + linked ToF picture (2026-10-15 evening) ---
        side = default_layer("l1", name="흐름", source="tof.depth", renderer="tof_side", params=dict(tof, inset=False, direction=400, base=35, vanish=2))
        view = default_layer("l2", name="원본", source="tof.depth", renderer="tof_sensor", params=tof, link="l1", w=0.3, h=0.3, visible=False)
        ups = [default_layer(f"l{3 + i}", name=r, source="audio.level", renderer=r, params=p) for i, (r, p) in enumerate([
            ("updist_meter", {"thickness": 9, "invert": "no", "min": 5, "max": 2}), ("updist_pulse", {"rings": 99, "decay": 0}),
            ("updist_trace", {"window": 1000, "fill": True}), ("updist_field", {"angle": 400, "bands": 1, "color_near": "#00FF00"})])]
        s7b = VisualStore(p3)
        s7b.apply({"op": "set", "id": cid6, "composition": {"name": "천장", "layers": [side, view, *ups]}}, final=True)
        c7 = s7b.composition(cid6)
        U = {l["renderer"]: l for l in c7["layers"]}
        check(all(l["source"] == "sensor.up_distance" for r, l in U.items() if r.startswith("updist_"))
              and U["updist_meter"]["params"]["thickness"] == 1.0 and U["updist_meter"]["params"]["invert"] is False
              and U["updist_pulse"]["params"]["rings"] == 12 and U["updist_pulse"]["params"]["decay"] == 0.1
              and U["updist_trace"]["params"]["window"] == 120 and U["updist_trace"]["params"]["fill"] is True
              and U["updist_field"]["params"]["angle"] == 359 and U["updist_field"]["params"]["bands"] == 2 and U["updist_field"]["params"]["color_near"] == "#00ff00"
              and set(U["updist_field"]["params"]) == set(UPDIST_PARAMS["updist_field"]),
              "ceiling sensor layers: always the up_distance source, settings (incl. flow angle 0–359) clamped, all present")
        check(c7["layers"][0]["params"]["direction"] == 359 and c7["layers"][0]["params"]["base"] == 35 and c7["layers"][0]["params"]["vanish"] == 5
              and not {"direction", "vanish"} & set(s7b.composition("side")["layers"][0]["params"]),
              "SIDE flow direction (0–359) and 소멸 기준선 (5–100 %) saved per layer, clamped, separate from the 기준선 'base'; the built-in SIDE has neither (as before)")
        check(c7["layers"][0]["params"]["inset"] is False and c7["layers"][1]["link"] == "l1" and c7["layers"][1]["visible"] is False
              and "inset" not in s7b.composition("side")["layers"][0]["params"],
              "SIDE 'flow only' and its linked ToF picture (hidden, kept) saved; the built-in SIDE has no inset setting (picture as before)")
        dup = {"op": "set", "id": cid6, "composition": {"name": "x", "layers": [side, view, dict(view, id="l9")]}}
        unknown = {"op": "set", "id": cid6, "composition": {"name": "x", "layers": [default_layer("l1", renderer="updist_field", params={"wind": 1})]}}
        before7 = p3.read_bytes()
        check(rejected(s7b, dup) and rejected(s7b, unknown) and p3.read_bytes() == before7, "rejected: a second linked picture for one SIDE layer, an unknown ceiling-layer setting")
        s7b.apply({"op": "set", "id": cid6, "composition": {"name": "x", "layers": [view, *ups]}}, final=True)
        check("link" not in s7b.composition(cid6)["layers"][0], "a link to a SIDE layer that is not there is cleared (no dangling link)")
        s7b.apply({"op": "set", "id": cid6, "composition": {"name": "천장", "layers": [side, view, *ups]}}, final=True)
        s7b.stop()
        s7c = VisualStore(p3)
        check(s7c.composition(cid6) == c7, "restart: ceiling sensor layers (incl. angle), SIDE flow only and the linked picture restored exactly")
        s7c.stop()

        # --- PULSE outline from the recent distance history + shared visual experiments (2026-10-15 night) ---
        fx = {"trail": {"on": True, "amount": 0.5, "duration": 9, "interval": 0.1}, "blur": {"on": True, "kind": "glow"}}
        pul = [default_layer("l1", name="p", renderer="updist_pulse", params={"shape": 3, "shape_gain": 0, "shape_s": 12.5}, effects=fx),
               default_layer("l2", name="old", renderer="updist_pulse", params={"rings": 3}),
               default_layer("l3", name="m", renderer="updist_trace", params={}, effects=fx)]
        s7d = VisualStore(p3)
        s7d.apply({"op": "set", "id": cid6, "composition": {"name": "pulse", "layers": pul}}, final=True)
        s7d.stop()
        s7e = VisualStore(p3)
        P = s7e.composition(cid6)["layers"]
        check(P[0]["params"]["shape"] == 1.0 and P[0]["params"]["shape_gain"] == 0.1 and P[0]["params"]["shape_s"] == 12.5
              and P[0]["effects"]["trail"] == {"on": True, "amount": 0.5, "duration": 5.0, "interval": 0.1} and P[0]["effects"]["blur"]["on"] is True
              and not {"shape", "shape_gain", "shape_s"} & set(P[1]["params"]) and "effects" not in P[1] and "effects" not in P[2],
              "PULSE: outline settings clamped and kept, shared effects saved; an older PULSE stays without them (plain circle); other ceiling types take no effects (VERTICAL METER takes them since 2026-11-10)")
        s7e.stop()

        # a file written before the experiments (here: the exhibition PC's visual.json of 2026-10-15 10:24) loads unchanged
        win = Path(sys.argv[1]) if len(sys.argv) > 1 else None   # python visual_check.py <a saved visual.json>
        if win:
            p4 = Path(d) / "old" / "visual.json"
            p4.parent.mkdir()
            p4.write_bytes(win.read_bytes())
            s8 = VisualStore(p4)
            old = json.loads(win.read_text(encoding="utf-8"))
            # settings added later with a default that draws exactly as before: 잔상 간격 (effects.trail.interval = 0)
            for c in old["compositions"].values():
                for layer in c["layers"]:
                    if isinstance(layer.get("effects"), dict) and isinstance(layer["effects"].get("trail"), dict):
                        layer["effects"]["trail"].setdefault("interval", 0.0)
                    # 평면 점 흔적 settings added 2026-10-30 (움직임 멈춤, 시야 표시, 쌓는 / 사라지는 방식): filled with their defaults
                    if layer.get("renderer") == "tof_topdown" and isinstance(layer.get("params"), dict):
                        for k, spec in ART_PARAMS["tof_topdown"].items():
                            layer["params"].setdefault(k, spec[-1])
            same = all({k: v for k, v in s8.composition(cid)["layers"][i].items()} == layer for cid in old["order"] for i, layer in enumerate(old["compositions"][cid]["layers"]))
            s8.apply({"op": "rename", "id": old["order"][0], "name": old["compositions"][old["order"][0]]["name"]}, final=True)
            resaved = json.loads(p4.read_text(encoding="utf-8"))
            check(same and s8.snapshot()["slots"] == old["slots"] and resaved["compositions"] == old["compositions"],
                  "a saved file from before loads with every layer value unchanged (only later settings at their as-before default, e.g. 잔상 간격 0) and saves back the same", win.name)
            s8.stop()

    width = max(len(n) for _, n, _ in results) + 2
    for ok, name, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}{detail}")
    failed = sum(not ok for ok, _, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
