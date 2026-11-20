"""2026-11-06: lobby (from the 2009 drawings) + exhibition layout spec for geometry.json. Run once from the repo root:

    python venues/m30/tools/make_lobby_layout.py

What it does (idempotent: running it again gives the same file):
- removes the four movable walls and the single hall-side glass solid: they become the layout spec (venue.py
  "exhibition layout") — big walls hinged at one end (user 2026-11-06: "큰 벽은 기둥에 힌지로 고정, 회전만"), the
  hall-side glass as 12 sliding panels (user: "로비쪽 면은 세로로 쭉 연결된 큰 슬라이딩 창문들"), and free small walls
  2,445 × 2,445 with the caster gap below (user).
- adds the lobby (홀 103), elevator hall, 안내 107, the toilet / stair core and 방풍실 108 from the 2009 drawings A-202 /
  A-501 (sfac_m30_hall_plan_elevation_2009.pdf, read at ±100 mm; the 2020 frame: X2 = −7,800, Y5 ≈ 13,200).
- widens the walkable outline to the lobby and the elevator hall.
"""

import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "geometry.json"
REF09 = "sfac_m30_hall_plan_elevation_2009.pdf A-202 지상1층 평면도 / A-501 홀평면도 (2009, 판독 ±100 mm)"
ST09 = "2009 도면 판독 (±100 mm)"
PHOTO = "2026-11-06 현장 사진"
ENTRANCE_BAYS = (2, 3)      # empty bays by default = the way in from the lobby (user 2026-11-06: 2·3번 칸)
BEAM_Z = (2900, 3300)       # H-beam underside / top: photos only (not measured)
BRACKET_Z = (2450, 2900)
NORTH_COL_Y0 = 19350     # the north columns' south face once pulled out of the north wall (500 deep, user 2026-11-06)
BEAM_ST = "2026-11-06 현장 사진 · 높이·단면 추정"
BEAM_REF = "사용자 사진(2026-11-06): 기둥에 H빔, 큰 벽 힌지가 여기에. 단면 200 × 400, 아랫면 2.9 m는 사진 추정"
# masts at the X3 / X5 columns (user 2026-11-06), all one shape (2026-11-07): from behind the column out to the big walls'
# hinge line, 480 wide on the column's centre (south corners: on the wall's face), 750 deep for the first 500 (behind the
# column), from there the underside slopes up to 200 at the tip (venue3d draws the slope; here a box, its lowest point);
# the H-beam on top (top = its underside). Not on X4Y7 (between the windows). [x0, y0, x1, y1, z0]
BRACKET_COLS = {"X3Y7": [-250, 260, 885, 740, 2150], "X3Y6": [-250, 6411, 885, 6891, 2150], "X3Y5": [-250, 12961.5, 885, 13441.5, 2150], "X3Y3": [-250, 19360.5, 885, 19840.5, 2150], "X5Y7": [10514, 260, 11651, 740, 2150], "X5Y6": [10514, 6360, 11651, 6840, 2150], "X5Y5": [10514, 12961.5, 11651, 13441.5, 2150], "X5Y3": [10514, 19360.5, 11651, 19840.5, 2150]}


def rect(sid, kind, r, status, ref, z0=0, z1=None):
    return {"id": sid, "kind": kind, "rect": r, "z0": z0, "z1": z1, "status": status, "ref": ref, "layer": None}


def main():
    g = json.loads(PATH.read_text(encoding="utf-8"))
    drop = {"movable-west", "movable-east-1", "movable-east-2", "movable-east-3", "glass-partition-west"}
    lobby_ids = {"lobby-core-west", "lobby-elevator-wall", "lobby-info-block", "lobby-glass-sw", "lobby-glass-se", "lobby-partition-end",
                 "vestibule-wall-w", "vestibule-wall-e", "vestibule-door-inner", "vestibule-door-outer", "lobby-bench",
                 "beam-west", "beam-east", "lobby-window-head", "lobby-transom", "door-north-head", "south-window-head", "south-soffit"} | {f"bracket-{c}" for c in BRACKET_COLS}
    g["solids"] = [s for s in g["solids"] if s["id"] not in drop and s["id"] not in lobby_ids]
    g["solids"] += [
        rect("lobby-core-west", "wall", [-10920, -250, -7570, 9171], ST09,
             f"{REF09} · 홀 서쪽: 화장실(남·여)·수신반실·계단실-2 묶음 (들어가지 않음)"),
        rect("lobby-elevator-wall", "wall", [-11070, 9171, -10770, 12800], ST09, f"{REF09} · 엘리베이터·E.D./P.D. 앞 벽 (문 닫힘)"),
        rect("lobby-info-block", "wall", [-11070, 12800, -540, 16200], ST09,
             f"{REF09} · 안내 107(창구 있음)·계단실-1 묶음. 들어가지 않음, 홀 쪽 면만 맞음"),
        rect("lobby-partition-end", "wall", [-540, -250, -250, 279], ST09, f"{REF09} · 슬라이딩 창 남쪽 끝 벽"),
        rect("lobby-glass-sw", "glass", [-7570, -250, -6407, -100], f"{ST09} · {PHOTO}", f"{REF09} · 홀 남쪽 바깥 유리 (14번 사진)"),
        rect("lobby-glass-se", "glass", [-1336, -250, -540, -100], f"{ST09} · {PHOTO}", f"{REF09} · 홀 남쪽 바깥 유리"),
        rect("vestibule-wall-w", "wall", [-6407, -3400, -6257, -100], ST09, f"{REF09} · 방풍실 108 서쪽 벽 (주출입구 4,500)"),
        rect("vestibule-wall-e", "wall", [-1486, -3400, -1336, -100], ST09, f"{REF09} · 방풍실 108 동쪽 벽"),
        rect("vestibule-door-inner", "glass", [-6257, -250, -1486, -100], f"{ST09} · 닫힘 가정", f"{REF09} · 방풍실 안쪽 자동문 (15번 사진)"),
        rect("vestibule-door-outer", "glass", [-6257, -3400, -1486, -3250], f"{ST09} · 닫힘 가정", f"{REF09} · 주출입구 바깥 자동문"),
        rect("lobby-bench", "fixture", [-1000, 8500, -600, 12600], f"{PHOTO} · 위치·크기 추정", "12번 사진: 홀 쪽 유리를 따라 놓인 긴 벤치", 0, 450),
        # H-beams along both column lines (user 2026-11-06 photos: 로비 쪽 기둥에 H빔, 반대도 같은 구조, 큰 벽 힌지가 여기에)
        rect("beam-west", "column", [450, 250, 650, 19851], BEAM_ST, BEAM_REF + " · 로비 쪽(X3 줄). 기둥과 겹치지 않고 마스트 위에만, 기둥에서 100 mm(사용자 2026-11-07)", BEAM_Z[0], BEAM_Z[1]),
        rect("beam-east", "column", [10850, 250, 11050, 19851], BEAM_ST, BEAM_REF + " · 반대쪽(X5 줄). 기둥과 겹치지 않고 마스트 위에만, 기둥에서 100 mm(사용자 2026-11-07)", BEAM_Z[0], BEAM_Z[1]),
    ] + [rect(f"bracket-{c}", "column", r[:4], BEAM_ST, BEAM_REF + " · 기둥마다 마스트(사용자: 기둥 뒤에서 나와 힌지까지, 기둥 끝부터 경사, 위에 H빔), 상자로 근사", r[4], BRACKET_Z[1]) for c, r in BRACKET_COLS.items()]
    # 2026-11-06 user corrections from the photos
    for sol in g["solids"]:
        if sol["id"] in ("col-X3Y3", "col-X5Y3"):   # the innermost columns stood half in the north wall: in front of it (user 2026-11-06)
            r = sol["rect"]
            sol["rect"] = [r[0], NORTH_COL_Y0, r[2], 19851]
            sol["status"] = "도면 실측 · 북쪽 벽 앞으로 당김(사용자 2026-11-06: 벽에 묻혀 있지 않음)"
        if sol["id"] == "door-north":      # "회색 세로는 작은 흰색 문으로": a 2.1 m door, wall above it
            sol.update(z1=2100, status="도면 실측(닫힘 가정) · 높이 2,100 추정")
        if sol["id"] in ("glass-door-south-1", "glass-door-south-2"):   # street windows up to the box above them
            sol.update(z1=3000, status="도면 실측 · 높이 3,000 추정(사진: 위에 흰 상자 구조)",
                       ref=sol["ref"].split(" — ")[0] + " — 사용자 2026-11-06: 출입문 없는 큰 창(흰 틀, 아래 큰 칸 + 위 작은 칸)")
        if sol["id"] == "curtain-east":    # "커튼도 좀 더 창문에 붙여줘": behind the column inner faces, at the windows
            sol["rect"] = [11300, 250, 11380, NORTH_COL_Y0]
            sol["status"] = "도면 실측(커튼 닫힘 가정) · 위치를 창 쪽으로(사용자 2026-11-06, 창 위치는 추정)"
        if sol["id"] == "curtain-west-north":
            sol["rect"] = [-150, 13501, -70, NORTH_COL_Y0]
            sol["status"] = "도면 실측(커튼 닫힘 가정) · 위치를 창 쪽으로(사용자 2026-11-06, 창 위치는 추정)"
    g["solids"] += [
        rect("door-north-head", "wall", [1250, 19851, 2250, 19891], ST09, "북쪽 문 위 벽 (문 높이 2,100 추정)", 2100, None),
        rect("south-window-head", "wall", [1450, 0, 9950, 90], f"{PHOTO} · 높이 추정", "길 쪽 창 위 벽", 3000, None),
        rect("south-soffit", "wall", [1450, 90, 9950, 500], f"{PHOTO} · 크기 추정", "사진: 길 쪽 창 위를 따라가는 흰 상자 구조(창 위 3.0–3.45 m, 방 쪽으로 약 0.4 m), 창 양끝에서 끊김(사용자 2026-11-06)", 3000, 3450),
    ]
    g["walkable"] = [[-7570, -100], [-462, -100], [-462, 90], [11149, 90], [11149, 19851], [-462, 19851], [-462, 12800],
                     [-10770, 12800], [-10770, 9171], [-7570, 9171]]
    big = "평면 위치·길이·두께 A-313 실측 · 힌지 끝 = 도면 회전 호의 중심 · 높이 추정"
    big_ref = "A-313 THK86 이동식벽체, 회전 호(점선). 사용자 2026-11-06: 큰 벽은 기둥 옆 힌지로 고정, 회전만. 높이는 자료 없음 → 사진(1·2·10번)에서 천장 아래로 1 m 넘게 비어 3,000 mm로 추정"
    g["layout"] = {
        "_comment": "전시 배치(venue.py exhibition layout). 큰 벽 = 힌지 회전, 작은 벽 = 자유 배치, 슬라이딩 창 = 열고 닫기. 상태는 simulator.json",
        "big_walls": [
            {"id": "movable-west", "hinge": [985, 13201], "closed_heading": 0, "swing": 1, "max_deg": 180, "default_deg": 0,
             "length": 6100, "thickness": 87, "height": 2800, "status": big, "ref": big_ref + " · 서쪽 벽, 도면 호는 90–180°까지. 2026-11-06 사용자 요청: 90°로 접었을 때 동쪽 북 벽과 겹치지 않게 힌지 150 mm 북쪽·길이 6,300으로 조정"},
            {"id": "movable-east-1", "hinge": [10414, 500], "closed_heading": 0, "swing": -1, "max_deg": 180, "default_deg": 0,
             "length": 5800, "thickness": 84, "height": 2800, "status": big, "ref": big_ref + " · 동쪽 남. 2026-11-06 사용자 요청: 창가 쪽이라 90°로 접으면 창·남쪽 벽과 겹쳐서 힌지를 안쪽(y 650)으로, 길이 5,900(끝 위치 그대로)"},
            {"id": "movable-east-2", "hinge": [10414, 6600], "closed_heading": 0, "swing": -1, "max_deg": 180, "default_deg": 0,
             "length": 6300, "thickness": 84, "height": 2800, "status": big, "ref": big_ref + " · 동쪽 가운데"},
            {"id": "movable-east-3", "hinge": [10414, 13201], "closed_heading": 0, "swing": -1, "max_deg": 180, "default_deg": 0,
             "length": 6100, "thickness": 84, "height": 2800, "status": big, "ref": big_ref + " · 동쪽 북. 2026-11-06 북쪽 기둥을 벽 앞으로 당겨서 기둥 받침에 닿지 않게 6,501 → 6,050"},
        ],
        # big walls: panel 2,800 over a 100 mm caster gap (top 2,900 = the masts' top), a foot across the free end (user
        # 2026-11-06); the hinge end on the mast's centre line, 100 mm out from the mast's tip on a round steel plate from it,
        # the plate on the mast's top; the panel runs 60 mm past the axis (hinge_overhang: the pins inside it) (2026-11-07)
        "big_wall_foot": {"length": 600, "width": 80, "gap": 100, "casters": 3, "hinge_overhang": 60, "status": "발 600 × 80·바퀴 틈 100·바퀴 3개 추정", "ref": "사용자 2026-11-06: 큰 벽은 힌지 반대쪽 끝에만 작은 가벽처럼 가로 받침대, 바퀴(가운데와 양끝보다 살짝 안쪽 3개). 받침대 높이만큼 바닥에 틈, 높이 살짝 낮춤, 받침대는 작은 벽보다 짧고 굵게"},
        "small_walls": {"width": 2445, "height": 2445, "gap": 100, "thickness": 80, "foot_length": 800, "foot_width": 40, "max_count": 3,
                        "default": [],
                        "status": "판 2,445 × 2,445 사용자 확인 · 바퀴 틈 100 mm·두께 80·발 800 × 40 추정(사용자 2026-11-06: 더 얇고 김)",
                        "ref": "사용자 2026-11-06: 작은 가벽 2445*2445 + 아래 바퀴 높이만큼 틈. 8번 사진의 T자 발·바퀴"},
        # 혈 / 맥 parking boxes (their start poses) fixed to their screen walls, parallel, right-aligned (user 2026-11-07)
        "parking": {"hyeol": {"wall": "small-3", "u": -422.5, "n": 1816, "dh": 0}, "maek": {"wall": "small-2", "u": 422.5, "n": -1328, "dh": 180}, "status": "사용자 2026-11-07: 주차 칸 · 기본 자리를 각자 가벽에 평행하게 붙여 따라다니게, 오른쪽 맞춤(칸 오른쪽 변 = 가벽 끝), 장치는 칸 가운데. 가벽에서의 거리는 시안 1 그대로"},
        # 12 bays of 1,050; bays 5–6 (south of column X3Y6) have no panel: the entrance (user 2026-11-06: no double
        # door, the panels fill the bays exactly and about two bays stay empty; which two = from the photo, to confirm)
        # the computer that drives 심 (user 2026-11-06: place it somewhere): a mid tower, size assumed
        "pc": {"size": [220, 480, 480], "default": {"x": 10150, "y": 9970, "heading": 270},
               "status": "크기 추정(미들 타워 220 × 480 × 480)", "ref": "사용자 2026-11-06: 심에 연결된 컴퓨터 본체를 어딘가에 놓을 수 있게"},
        "windows": {"bays": 12, "bay_y0": 279, "bay_w": 1050, "x": [-540, -462], "panels": [f"lobby-window-{i + 1}" for i in range(12 - len(ENTRANCE_BAYS))],
                    "default_bays": [b for b in range(1, 13) if b not in ENTRANCE_BAYS], "height": None,
                    "status": "평면 A-313 실측 (1,050 × 12칸), 판 10장",
                    "ref": "A-313 이동식 유리 파티션 12EA / A-501 MOVABLE PARTITION(12EA). 사용자 2026-11-06: 바닥에서 천장까지 한 칸짜리 판(짙은 갈색 틀, 반투명 띠), 위에 벽 없음, 한 레일에서 좌우로만 밀림(열려면 다른 판을 밂)"},
    }
    g["ceiling"]["detail"] = {"type": "mesh", "area": [-462, 90, 11149, 19851], "grid_mm": 1200, "lamp_spacing_mm": 2400, "lamp_z": 4700,
                              "note": "흰 격자 틀 + 철망 천장(17번 사진), 철망 위 둥근 창고등(사용자). 격자 1,200·등 2,400 간격·높이 4.7 m는 화면 표시용 추정. 센서는 철망 면 4.2 m를 그대로 씀"}
    note = "로비 높이: 자료 없음, 갤러리와 같은 4.2 m로 둠"
    if note not in g["unknown"]:
        g["unknown"] = [u for u in g["unknown"] if not u.startswith("이동식벽체의 높이")] + [
            "큰 이동식벽체 높이(3,000 mm 추정)", "작은 가벽 바퀴 틈·두께·발 크기", note, "철망 위 창고등 위치·높이, 철망을 통과하는 센서 반사"]
    g["approximations"] = [a for a in g["approximations"] if not a.startswith("이동식벽체(THK86) 4개") and not a.startswith("문·유리문·이동식 유리 파티션")] + [
        "큰 이동식벽체 4개: A-313 위치·크기, 힌지 끝 고정·회전만(사용자). 각도는 전시 배치에서 바꿈",
        "홀 쪽 슬라이딩 창 12장: 열림 = 이웃 칸 뒤로 겹침. 문·유리문·방풍실 자동문은 닫힌 상태",
        "로비·안내·화장실 코어·방풍실: 2009 도면 판독(±100 mm), 들어갈 수 없는 실은 덩어리로"]
    if "로비" not in g.get("status_note", ""):
        g["status_note"] += " 2026-11-06: 2009 도면으로 로비·방풍실 추가, 이동식벽체·슬라이딩 창은 전시 배치(회전·열고 닫기)로."
    PATH.write_text(json.dumps(g, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("written", PATH)


if __name__ == "__main__":
    main()
