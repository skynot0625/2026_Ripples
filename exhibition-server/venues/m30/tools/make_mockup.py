#!/usr/bin/env python3
"""Exhibition mock-up for M30 (verification only, NOT installation values). Writes venues/m30/mockup.json, a separate
layer that geometry.json includes ("layers"); geometry.json itself is not touched.

    python venues/m30/tools/make_mockup.py

2026-11-06 (user: "심 모델링도 심 시뮬레이터에 있는 거로", "블라인드들은 없애"): the central structure 심 is now the same
model as the 심 LED simulator (/sim, sim.html): a 2,120 × 1,835.97 hexagonal plate (six 1,060 triangles, pointy
toward ±x) with 17 rows A–Q of holes on a 2,120/17 triangular grid, 180 vertical LED strips (rows A 65 … Q 50 LEDs at
50 mm, LED 1 in the hole), the plate tilted so the strip ends are level 0.50–0.54 m above the floor (centre 3,335 mm,
row A side higher), the beam under the empty I row and the tapering four-leg tower with rings. Row A faces +y (north),
the same as the /sim data pattern. The tilted plate is stored as one horizontal 24 mm slab per row band (the venue
format extrudes footprints vertically); the tower's X braces are not stored (thin diagonals, not vertical prisms).
Blinds removed. People unchanged (they are drawn with the 오블렛 person model in /venue3d).
History: the first mock-up (2026-10-14: square plate, slanted strips) and the second (4 posts, 48 strips, 3,000 mm,
6 blinds) were replaced on the user's corrections.
"""
import json
import math
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "mockup.json"
STATUS = "검증용 목업 (설치 확정값 아님)"
REF = "심 = /sim 심 LED 시뮬레이터와 같은 모델(2026-11-06 사용자). 치수 고정값(11-01)·사진 근사 탑. make_mockup.py로 생성"

CX, CY = 5700.0, 9970.0          # centre of the plate on the plan
ROWS = "ABCDEFGHIJKLMNOPQ"
LEDS = dict(A=65, B=64, C=63, D=62, E=61, F=60, G=59, H=58, I=0, J=57, K=56, L=55, M=54, N=53, O=52, P=51, Q=50)
EMPTY = set("A5 C6 E7 G7 G8 G9 H7 H8 H9 H10 J7 J8 J9 J10 K7 K8 K9 M7 O6 Q5".split()) | {f"I{k + 1}" for k in range(17)}
PITCH = 2120 / 17
ROWSTEP = PITCH * math.sqrt(3) / 2
D = 50.0                          # LED pitch
PLATE_T = 24.0
BEAM_W = 60.0
BOTTOM_GAP = 500.0                # lowest strip end above the floor (user 11-01)
STRIP = 14.0                      # strip (cable + LED beads) cross-section for the geometry
HALF_W, HALF_H = 1060.0, 1060.0 * math.sqrt(3) / 2   # hexagon: corners at ±x, flat sides at ±z

# the same arithmetic as sim.html
STRIPS = []
for r in range(17):
    n = 9 + min(r, 16 - r)
    z = (r - 8) * ROWSTEP
    for k in range(n):
        name = f"{ROWS[r]}{k + 1}"
        if name in EMPTY or not LEDS[ROWS[r]]:
            continue
        STRIPS.append({"name": name, "x": (k - (n - 1) / 2) * PITCH, "z": z, "n": LEDS[ROWS[r]]})
plate0 = lambda z: -(z / ROWSTEP) * (15 / 16) * D          # plate centre height offset (row A, z < 0, higher)
HC_SIM = BOTTOM_GAP - min(plate0(s["z"]) + PLATE_T / 2 - (s["n"] - 1) * D for s in STRIPS)   # /sim: ends 0.50 m up
# M30 only (user 2026-11-06 "바닥에서의 높이를 줄여, 끝을 몇 개 이 시뮬에서만 잘라도 돼"): highest point (row A hole) at
# TOP_MAX, same tilt and pitch; each strip loses the LEDs that would come closer than MIN_END to the floor
TOP_MAX, MIN_END = 3000.0, 50.0
HC = HC_SIM - (max(plate0(s["z"]) for s in STRIPS) + PLATE_T / 2 + HC_SIM - TOP_MAX)
plate_y = lambda z: HC + plate0(z)
top_y = lambda s: plate_y(s["z"]) + PLATE_T / 2

# /sim (x right, z toward row Q) → venue plan (x, y): y = CY − z, so row A (z < 0) is north
P = lambda x, z: [round(CX + x, 1), round(CY - z, 1)]
rect = lambda x0, y0, x1, y1: [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]
HEX = [(HALF_W, 0), (HALF_W / 2, HALF_H), (-HALF_W / 2, HALF_H), (-HALF_W, 0), (-HALF_W / 2, -HALF_H), (HALF_W / 2, -HALF_H)]


def clip(poly, z0, z1):
    """Hexagon (sim x, z) cut to the band z0 ≤ z ≤ z1 (Sutherland–Hodgman, two horizontal lines)."""
    def cut(pts, keep, at):
        out = []
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            ka, kb = keep(a), keep(b)
            if ka:
                out.append(a)
            if ka != kb:
                t = (at - a[1]) / (b[1] - a[1])
                out.append((a[0] + t * (b[0] - a[0]), at))
        return out
    return cut(cut(poly, lambda p: p[1] >= z0, z0), lambda p: p[1] <= z1, z1)


def solids():
    out = []

    def mk(sid, kind, z0, z1, note, **geo):
        out.append({"id": sid, "kind": kind, **geo, "z0": round(z0, 1), "z1": round(z1, 1), "status": STATUS,
                    "ref": f"{REF} · {note}", "layer": "exhibition_mockup"})
    # tilted plate: one horizontal slab per row band (the outer rows reach the flat sides), at that band's height
    for r in range(17):
        zc = (r - 8) * ROWSTEP
        z0 = -HALF_H if r == 0 else zc - ROWSTEP / 2
        z1 = HALF_H if r == 16 else zc + ROWSTEP / 2
        band = clip(HEX, z0, z1)
        if len(band) >= 3:
            y = plate_y(zc)
            mk(f"sim-plate-{ROWS[r]}", "mock_plate", y - PLATE_T / 2, y + PLATE_T / 2, f"윗판 {ROWS[r]}행 띠 (기울어진 판, 띠마다 수평)",
               poly=[P(x, z) for x, z in band])
    # beam under the I row
    y0 = plate_y(0) - PLATE_T / 2
    mk("sim-beam-I", "mock_frame", y0 - BEAM_W, y0, "I행 아래 보", rect=rect(CX - 980, CY - BEAM_W / 2, CX + 980, CY + BEAM_W / 2))
    # tower: four tapering legs (half width 360 → 200 mm) in 6 vertical pieces each, rings at 7 levels
    top = y0 - BEAM_W
    at = lambda h: 360 + (200 - 360) * (h / top)
    for i, (sx, sz) in enumerate(((1, 1), (-1, 1), (-1, -1), (1, -1))):
        for q in range(6):
            h0, h1 = q * top / 6, (q + 1) * top / 6
            w = at((h0 + h1) / 2)
            mk(f"sim-leg-{i + 1}-{q + 1}", "mock_frame", h0, h1, "탑 다리 (사진 근사, 위로 갈수록 좁아짐)",
               rect=rect(CX + sx * w - 20, CY - sz * w - 20, CX + sx * w + 20, CY - sz * w + 20))
    for q in range(7):
        h = q * top / 6
        w = at(h)
        for j, (a, b) in enumerate((((-w, w), (w, w)), ((w, -w), (w, w)), ((-w, -w), (w, -w)), ((-w, -w), (-w, w)))):
            x0, x1 = sorted((a[0], b[0]))
            z0, z1 = sorted((a[1], b[1]))
            mk(f"sim-ring-{q + 1}-{j + 1}", "mock_frame", max(0, h - 12), max(24, h + 12), "탑 가로 링",
               rect=rect(CX + x0 - 12, CY - z1 - 12, CX + x1 + 12, CY - z0 + 12))
    # 180 strips: LED 1 in the hole (plate middle) down to the last LED
    for s in STRIPS:
        t = top_y(s)
        n = min(s["n"], int((t - MIN_END) // D) + 1)   # LEDs kept in this simulation (the real strip has s["n"])
        x, y = P(s["x"], s["z"])
        mk(f"led-{s['name']}", "mock_led", t - (n - 1) * D - 15, t, f"스트립 {s['name']} LED {n}개 (실물 {s['n']}개, 이 시뮬에서 {s['n'] - n}개 자름)",
           rect=rect(x - STRIP / 2, y - STRIP / 2, x + STRIP / 2, y + STRIP / 2))
    return out


# a few people (one in the south half, two in the north half), each wandering slowly inside its own area
PEOPLE = {
    "status": STATUS, "ref": "일반적인 성인 크기·걸음의 목업값", "radius_mm": 250, "height_mm": 1700, "speed_mm_s": [250, 400],
    "note": "일반적인 성인 크기·걸음의 목업값(확정값 아님). 위치는 서버의 단일 시뮬레이션 상태(저장하지 않음). /venue3d는 오블렛 전시 목업의 사람 모델로 그림",
    "list": [
        {"id": "person-1", "start": [8200, 4200], "heading": 30, "area": [7000, 2400, 9800, 7600]},
        {"id": "person-2", "start": [3600, 14500], "heading": 200, "area": [2000, 12600, 5200, 17400]},
        {"id": "person-3", "start": [8000, 15200], "heading": 120, "area": [6600, 12600, 9600, 17200]},
    ],
}


def main():
    data = {"format": "ripples-venue-layer/1", "id": "exhibition_mockup", "status": STATUS, "ref": REF,
            "note": f"심 1개(/sim 모델, M30에서만 가장 높은 곳 {TOP_MAX:.0f} mm로 낮추고 바닥에 닿는 끝 LED를 자름: 윗판 중심 {HC:.0f} mm, 스트립 180개), 사람 3명. 블라인드 없음(2026-11-06). 목업값, 설치 확정값 아님",
            "center": [CX, CY], "solids": solids(), "people": PEOPLE}
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{OUT}: {len(data['solids'])} mock-up solids (plate centre {HC:.1f} mm), {len(PEOPLE['list'])} people")


if __name__ == "__main__":
    main()
