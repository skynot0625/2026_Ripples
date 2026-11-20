#!/usr/bin/env python3
"""Reproducible numeric checks for the sensor-like ToF mock (mock_tof.py).

    python mock_check.py            # seed 7, 90 frames (3 s at 30 fps); exit code 1 if a check fails

Measures the properties the mock is meant to have. It does not compare against a real Arducam B0410 (no captures yet).
"""

from __future__ import annotations

import sys
import time

import numpy as np

import mock_tof as mt

CONF_MIN = 30  # the viewer's default confidence threshold (index.html / server.py)


def main(seed: int = 7, n: int = 90) -> int:
    cam = mt.MockToF(seed=seed)
    depths, confs, labels, mixed = [], [], [], []
    t0 = time.perf_counter()
    for k in range(n):
        d, c = cam.frame(k / 30)
        depths.append(d); confs.append(c); labels.append(cam.last_labels.copy()); mixed.append(cam.last_mixed.copy())
    ms = (time.perf_counter() - t0) / n * 1000
    D, C, L, X = np.stack(depths), np.stack(confs), np.stack(labels), np.stack(mixed)
    valid = (D > 0) & (C >= CONF_MIN)
    results = []

    def check(name, ok, value):
        results.append((name, bool(ok), value))

    # 1. shape and types
    check("frame shape 180x240, float32", D.shape[1:] == (180, 240) and D.dtype == np.float32 and C.dtype == np.float32, f"{D.shape[1:]} {D.dtype}")
    # 2. depth range and beyond-range handling
    # The nearest real surface in this scene is ~1 m away. Away from edges a valid depth under 500 mm would be a
    # wrapped value; on edges, mixing a near and a far return in phase can land outside both (a real ToF artifact).
    flat_valid = valid & ~X
    near_edge = valid & X & (D < 500)
    check("valid depth within 500–4000 mm away from edges (no wrapped values)", D[flat_valid].min() >= 500 and D[valid].max() <= mt.MAX_RANGE_MM,
          f"{D[flat_valid].min():.0f}–{D[valid].max():.0f} mm; edge mixes under 500 mm: {near_edge.sum()} px in {n} frames")
    corridor = (L == mt.CORRIDOR) & ~X  # all four sub-rays on the corridor (no mixed edge pixels)
    check("beyond 4 m (corridor) never valid", not valid[corridor].any() and (D[corridor] == 0).mean() > 0.95,
          f"corridor depth=0 {100 * (D[corridor] == 0).mean():.1f}%, valid {100 * valid[corridor].mean():.1f}%")
    # 3. invalid ratio and its link to confidence
    zero = D == 0
    check("depth=0 only where confidence < threshold", (C[zero] < CONF_MIN).mean() > 0.99, f"{100 * (C[zero] < CONF_MIN).mean():.2f}% of depth=0 pixels have conf<{CONF_MIN}")
    check("invalid share (depth=0 or conf<30) between 10% and 60%", 0.10 < 1 - valid.mean() < 0.60, f"{100 * (1 - valid.mean()):.1f}%")
    dark = np.isin(L, [mt.POSTER, mt.BOX]); bright = L == mt.BACK
    check("dark surfaces (poster, box) lose more pixels than the bright back wall",
          (1 - valid[dark].mean()) > (1 - valid[bright].mean()) + 0.2,
          f"invalid dark {100 * (1 - valid[dark].mean()):.1f}% vs back wall {100 * (1 - valid[bright].mean()):.1f}%")
    # 4. temporal stability: static background vs moving targets
    static = np.isin(L, mt.STATIC_IDS).all(axis=0) & ~X.any(axis=0)
    both_valid = valid[1:] & valid[:-1]
    diff = np.abs(D[1:] - D[:-1])
    s_mask = both_valid & static[None]
    moving = np.isin(L[1:], [mt.PERSON, mt.OBJECT]) | np.isin(L[:-1], [mt.PERSON, mt.OBJECT])
    m_mask = moving & (D[1:] > 0) & (D[:-1] > 0)
    s90, m90 = np.percentile(diff[s_mask], 90), np.percentile(diff[m_mask], 90)
    s_mean, m_mean = diff[s_mask].mean(), diff[m_mask].mean()
    check("static background changes less frame to frame than moving targets", s90 * 2 < m90 and s_mean * 2 < m_mean,
          f"|Δ| p90 static {s90:.1f} vs moving {m90:.1f} mm, mean {s_mean:.1f} vs {m_mean:.1f} mm")
    flip = (valid[1:] != valid[:-1]) & static[None]
    check("static pixels rarely flip valid/invalid between frames", flip.mean() < 0.05, f"{100 * flip.mean():.2f}% per frame")
    # 5. along one surface (the floor, near → far): noise and missing pixels grow with distance
    floor = (L == mt.FLOOR).all(axis=0) & static
    true_r = np.where(floor, np.median(np.where(D > 0, D, np.nan), axis=0), np.nan)  # per-pixel typical distance
    edges_ = np.nanpercentile(true_r[floor], [0, 33, 67, 100])
    stds, miss = [], []
    for lo, hi in zip(edges_[:-1], edges_[1:]):
        b = floor & (true_r >= lo) & (true_r <= hi)
        vb = valid[:, b]
        enough = vb.sum(axis=0) >= 10  # temporal std only where a pixel is valid in at least 10 frames
        db = np.where(vb, D[:, b], np.nan)[:, enough]
        stds.append(float(np.nanmedian(np.nanstd(db, axis=0))) if db.size else float("nan"))
        miss.append(float(1 - vb.mean()))
    check("depth noise grows with distance (floor)", all(a < b for a, b in zip(stds, stds[1:])),
          " < ".join(f"{s:.1f}" for s in stds) + f" mm over {edges_[0]:.0f}–{edges_[-1]:.0f} mm")
    check("missing pixels grow with distance (floor)", all(a < b for a, b in zip(miss, miss[1:])), " < ".join(f"{100 * m:.1f}%" for m in miss))
    # 6. edges: mixed pixels give intermediate (flying) depths or gaps
    edge = X & (D > 0)
    check("edge pixels exist and some are flying/intermediate", X.mean() > 0.01 and edge.sum() > 0, f"edge pixels {100 * X.mean():.1f}%, valid among edges {100 * valid[X].mean():.1f}% vs non-edge {100 * valid[~X].mean():.1f}%")
    check("edges lose more pixels than flat areas", valid[X].mean() < valid[~X].mean(), f"{100 * valid[X].mean():.1f}% < {100 * valid[~X].mean():.1f}%")
    # 7. moving targets move continuously (no jumps between frames)
    cx = [np.argwhere(l == mt.PERSON)[:, 1].mean() for l in L if (l == mt.PERSON).any()]
    step = np.abs(np.diff(cx)).max() if len(cx) > 1 else 0
    check("person moves continuously (max centre step < 3 px/frame)", step < 3, f"{step:.2f} px")
    # 8. reproducible and fast enough for 30 fps
    again = mt.MockToF(seed=seed)
    same = all(np.array_equal(again.frame(k / 30)[0], depths[k]) for k in range(5))
    check("same seed → same frames", same, "first 5 frames identical" if same else "differ")
    check("generation time < 25 ms/frame (30 fps budget 33 ms)", ms < 25, f"{ms:.1f} ms/frame on this machine")

    width = max(len(r[0]) for r in results)
    for name, ok, value in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {value}")
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed (seed {seed}, {n} frames)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
