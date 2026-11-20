#!/usr/bin/env python3
"""Draw a venue's geometry.json over its source drawing (verification image). Development tool only.

    python venues/tools/overlay_pdf.py m30   (needs PyMuPDF: pip install pymupdf — not a runtime dependency)

The drawing → venue transform is stored in geometry.json ("drawing_transform"): page point = origin_pt + mm / mm_per_pt
(y flipped). Solids are drawn in red, the walkable outline in blue, device start positions as circles.
"""
import json
import sys
from pathlib import Path

import fitz  # PyMuPDF

ROOT = Path(__file__).resolve().parents[1]


def main(venue_id: str) -> None:
    g = json.loads((ROOT / venue_id / "geometry.json").read_text(encoding="utf-8"))
    tr = g["drawing_transform"]
    doc = fitz.open(ROOT / venue_id / tr["file"])
    page = doc[tr.get("page", 0)]
    derot = page.derotation_matrix
    ox, oy, k = tr["origin_pt"][0], tr["origin_pt"][1], tr["mm_per_pt"]
    P = lambda x, y: fitz.Point(ox + x / k, oy - y / k) * derot  # venue mm → displayed page pt → unrotated page pt

    def rect_poly(s):
        if "rect" in s:
            x0, y0, x1, y1 = s["rect"]
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return s["poly"]

    shape = page.new_shape()
    for s in g["solids"]:
        pts = [P(*q) for q in rect_poly(s)]
        shape.draw_polyline(pts + [pts[0]])
        shape.finish(color=(1, 0, 0), fill=(1, 0, 0), fill_opacity=0.35, width=0.6)
    w = [P(*q) for q in g["walkable"]]
    shape.draw_polyline(w + [w[0]])
    shape.finish(color=(0, 0.3, 1), width=0.8, dashes="[3 2] 0")
    for d, p in g["start"].items():
        shape.draw_circle(P(p["x"], p["y"]), 300 / k)
        shape.finish(color=(0, 0.5, 0), width=0.8)
    shape.commit()
    clip = fitz.Rect(*tr["clip_pt"]) if "clip_pt" in tr else None
    out = ROOT / venue_id / "verify" / f"overlay_{tr['name']}.png"
    out.parent.mkdir(exist_ok=True)
    page.get_pixmap(dpi=tr.get("dpi", 150), clip=clip).save(out)
    print(out)


if __name__ == "__main__":
    main(sys.argv[1])
