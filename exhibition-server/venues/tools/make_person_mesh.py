"""Person model for /venue3d (2026-11-06): the person used in the 오블렛 exhibition mock-up
(minba/public/oblet/models/dezyne_3d-sad-373.glb, 46 MB skinned glTF) baked once into a small static mesh.

    python venues/tools/make_person_mesh.py <glb> static/person_mesh.bin

- skinning applied with the skeleton as stored in the file (the pose the 오블렛 mock-up shows), all meshes merged
- height scaled to 1,650 mm like the 오블렛 mock-up, feet (lowest point) at z = 0, facing +y on the plan
- simplified by vertex clustering (CELL mm) so the browser draws it without a 3D library
Output (little-endian): uint32 triangle count, then per vertex int16 x, y, z (mm, plan: x right, y forward, z up) and
int8 nx, ny, nz, pad — 3 vertices per triangle. Model licence not checked (as in the 오블렛 log): check before release.
"""

import json
import struct
import sys
from pathlib import Path

import numpy as np

HEIGHT_MM = 1650.0
CELL = 3.5  # mm clustering cell (~55k triangles, ~1.6 MB; coarser cells leave holes)
COMP = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def load(path):
    f = Path(path).read_bytes()
    jl = struct.unpack("<I", f[12:16])[0]
    j = json.loads(f[20:20 + jl])
    off = 20 + jl
    bl = struct.unpack("<I", f[off:off + 4])[0]
    binc = f[off + 8:off + 8 + bl]

    def acc(i):
        a = j["accessors"][i]
        bv = j["bufferViews"][a["bufferView"]]
        dt, n = COMP[a["componentType"]], NCOMP[a["type"]]
        start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        stride = bv.get("byteStride")
        size = np.dtype(dt).itemsize * n
        if stride and stride != size:
            raw = np.frombuffer(binc, np.uint8, count=stride * a["count"], offset=start).reshape(a["count"], stride)[:, :size]
            arr = np.frombuffer(raw.tobytes(), dt).reshape(a["count"], n)
        else:
            arr = np.frombuffer(binc, dt, count=a["count"] * n, offset=start).reshape(a["count"], n)
        if a.get("normalized") and dt != np.float32:
            arr = arr.astype(np.float32) / np.iinfo(dt).max
        return arr
    return j, acc


def local(n):
    if "matrix" in n:
        return np.array(n["matrix"], float).reshape(4, 4).T
    t = np.array(n.get("translation", [0, 0, 0]), float)
    x, y, z, w = n.get("rotation", [0, 0, 0, 1])
    s = np.array(n.get("scale", [1, 1, 1]), float)
    R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    M = np.eye(4)
    M[:3, :3] = R * s
    M[:3, 3] = t
    return M


def main(src, out):
    j, acc = load(src)
    nodes = j["nodes"]
    world = [None] * len(nodes)
    parent = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}

    def W(i):
        if world[i] is None:
            world[i] = (W(parent[i]) if i in parent else np.eye(4)) @ local(nodes[i])
        return world[i]
    tris = []
    for ni, n in enumerate(nodes):
        if "mesh" not in n:
            continue
        skin = j["skins"][n["skin"]] if "skin" in n else None
        if skin:
            ibm = acc(skin["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)
            jm = np.array([W(jn) @ ibm[k] for k, jn in enumerate(skin["joints"])])
        for p in j["meshes"][n["mesh"]]["primitives"]:
            pos = acc(p["attributes"]["POSITION"]).astype(float)
            ph = np.c_[pos, np.ones(len(pos))]
            if skin:
                J = acc(p["attributes"]["JOINTS_0"]).astype(int)
                Wt = acc(p["attributes"]["WEIGHTS_0"]).astype(float)
                Wt /= np.maximum(Wt.sum(1, keepdims=True), 1e-9)
                v = np.zeros((len(pos), 4))
                for k in range(4):
                    v += Wt[:, k:k + 1] * np.einsum("nij,nj->ni", jm[J[:, k]], ph)
            else:
                v = (W(ni) @ ph.T).T
            idx = acc(p["indices"]).reshape(-1) if "indices" in p else np.arange(len(pos))
            tris.append(v[:, :3][idx.reshape(-1, 3)])
    T = np.concatenate(tris)                     # (n, 3, 3) glTF: y up, +z forward
    P = T.reshape(-1, 3)
    lo, hi = P[:, 1].min(), P[:, 1].max()
    k = HEIGHT_MM / (hi - lo)
    # glTF (x, y up, z forward) → plan (x, y forward, z up), mm, feet at 0, centred on the plan
    pl = np.stack([P[:, 0], P[:, 2], P[:, 1] - lo], 1) * k
    pl[:, :2] -= (pl[:, :2].min(0) + pl[:, :2].max(0)) / 2
    q = np.round(pl / CELL).astype(np.int64)
    key, inv = np.unique(q, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    cent = np.zeros((len(key), 3))
    np.add.at(cent, inv, pl)
    cent /= np.bincount(inv, minlength=len(key))[:, None]
    tri = inv.reshape(-1, 3)
    tri = tri[(tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])]
    tri = np.unique(np.sort(tri, 1), axis=0, return_index=True)[1]
    tri = inv.reshape(-1, 3)[np.sort(tri)]
    tri = tri[(tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])]
    a, b, c = cent[tri[:, 0]], cent[tri[:, 1]], cent[tri[:, 2]]
    fn = np.cross(b - a, c - a)
    fn /= np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-9)
    vn = np.zeros_like(cent)                    # smooth normals per clustered vertex
    for kk in range(3):
        np.add.at(vn, tri[:, kk], fn)
    vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-9)
    rec = np.zeros(len(tri) * 3, dtype=[("p", "<i2", 3), ("n", "i1", 3), ("pad", "i1")])
    rec["p"] = np.round(cent[tri.reshape(-1)]).astype(np.int16)
    rec["n"] = np.round(vn[tri.reshape(-1)] * 127).astype(np.int8)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_bytes(struct.pack("<I", len(tri)) + rec.tobytes())
    print(f"{len(T)} → {len(tri)} triangles, height {pl[:, 2].max():.0f} mm, footprint {np.ptp(pl[:, 0]):.0f} × {np.ptp(pl[:, 1]):.0f} mm, {Path(out).stat().st_size} bytes")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
