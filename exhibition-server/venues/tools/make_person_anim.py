"""Animated person for /venue3d (2026-11-06, user: "관객 모션이 없어서 무서움, 애니메이션 있는 사람 넣어줘").

    python venues/tools/make_person_anim.py <glb> static/person_anim.bin [--walk Walk] [--idle Idle] [--frames 16] [--turn 180]

Used (2026-11-06): the three.js examples Ready Player Me avatar (readyplayer.me.glb: a man in a coat, hat and shoes;
https://threejs.org/examples/models/gltf/readyplayer.me.glb) moved by the Idle / Walk clips of the three.js Soldier.glb
(Mixamo) through bone directions (--clips-from, the rigs share Mixamo bone names), simplified with --cell 12:

    python -I venues/tools/make_person_anim.py readyplayer.me.glb static/person_anim.bin --clips-from Soldier.glb --auto-face --cell 12 \
        --drop Beard,Headwear      # user: no hat / moustache

Tried and dropped: Soldier itself (combat suit), CesiumMan (can-shaped head), a generated walk for the 오블렛 person.
Licences (Ready Player Me avatar, Mixamo motion): check before any public release.
Output (little-endian): b"RPA1", uint32 triangles, uint32 frames (idle + walk), float32 stride_mm (distance walked in
one cycle), then per frame per vertex int16 x, y, z (mm) + int8 nx, ny, nz + pad, 3 vertices per triangle.
"""
import argparse
import json
import struct
from pathlib import Path

import numpy as np

HEIGHT_MM = 1650.0
STRIDE_MM = 1300.0   # one walk cycle (two steps) at an easy pace
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
        stride, size = bv.get("byteStride"), np.dtype(dt).itemsize * n
        if stride and stride != size:
            raw = np.frombuffer(binc, np.uint8, count=stride * a["count"], offset=start).reshape(a["count"], stride)[:, :size]
            arr = np.frombuffer(raw.tobytes(), dt).reshape(a["count"], n)
        else:
            arr = np.frombuffer(binc, dt, count=a["count"] * n, offset=start).reshape(a["count"], n)
        if a.get("normalized") and dt != np.float32:
            arr = arr.astype(np.float32) / np.iinfo(dt).max
        return arr
    return j, acc


def rot(axis, a):
    """Rotation matrix about a unit axis by a radians (Rodrigues)."""
    x, y, z = axis / np.linalg.norm(axis)
    c, s, C = np.cos(a), np.sin(a), 1 - np.cos(a)
    return np.array([[c + x * x * C, x * y * C - z * s, x * z * C + y * s], [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                     [z * x * C - y * s, z * y * C + x * s, c + z * z * C]])


def joint_pos(j, acc, name):
    v = None
    nodes = j["nodes"]
    parent = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}

    def Wm(i):
        n = nodes[i]
        L = np.array(n["matrix"], float).reshape(4, 4).T if "matrix" in n else trs(n.get("translation", [0, 0, 0]), n.get("rotation", [0, 0, 0, 1]), n.get("scale", [1, 1, 1]))
        return (Wm(parent[i]) if i in parent else np.eye(4)) @ L
    i = next(k for k, n in enumerate(nodes) if n.get("name") == name)
    return Wm(i)[:3, 3]


CHAINS = [("Spine1", "Spine2"), ("Spine2", "Neck"), ("Neck", "Head"),
          ("LeftShoulder", "LeftArm"), ("LeftArm", "LeftForeArm"), ("LeftForeArm", "LeftHand"),
          ("RightShoulder", "RightArm"), ("RightArm", "RightForeArm"), ("RightForeArm", "RightHand"),
          ("LeftUpLeg", "LeftLeg"), ("LeftLeg", "LeftFoot"), ("LeftFoot", "LeftToeBase"),
          ("RightUpLeg", "RightLeg"), ("RightLeg", "RightFoot"), ("RightFoot", "RightToeBase")]


def body_basis(P):
    """Columns: towards the left, up, forward — from the legs and the chest of a pose (bone name → position)."""
    lat = P["LeftUpLeg"] - P["RightUpLeg"]
    up = P["Spine2"] - P["Hips"]
    lat /= np.linalg.norm(lat)
    up = up - lat * np.dot(up, lat)
    up /= np.linalg.norm(up)
    return np.stack([lat, up, np.cross(lat, up)], 1)


def retarget_dirs(tj, tacc, sj, sacc, clip, times, strip, stand=False):
    """Poses of model tj following the clip of model sj by bone directions (rigs with the same Mixamo bone names but
    different rests and bone axes): in the body frame of each, every segment of CHAINS is turned to point the way the
    source segment points; the body rises / sinks with the source hips (scaled by leg length)."""
    def names_pos(j, acc, clip, t, strip_):
        pose(j, acc, clip, t)
        return {k.replace(strip_, ""): v[:3, 3].copy() for k, v in LAST.items() if k != "__world__"}
    S0 = names_pos(sj, sacc, None, 0.0, strip)
    pose(tj, tacc, None, 0.0)
    T0w = LAST["__world__"]
    T0 = {k: v[:3, 3].copy() for k, v in LAST.items() if k != "__world__"}
    Bs, Bt = body_basis(S0), body_basis(T0)
    M = Bt @ Bs.T
    legs = np.linalg.norm(S0["LeftUpLeg"] - S0["LeftFoot"]), np.linalg.norm(T0["LeftUpLeg"] - T0["LeftFoot"])
    idx = {n.get("name"): i for i, n in enumerate(tj["nodes"])}
    kids = {i: n.get("children", []) for i, n in enumerate(tj["nodes"])}

    def subtree(i):
        out = [i]
        for c in kids[i]:
            out += subtree(c)
        return out
    out = []
    for t in times:
        St = names_pos(sj, sacc, clip, t, strip)
        Bs_t = body_basis(St)   # the source body may turn / lean in the clip: keep its facing out, the target faces forward
        Wt = [w.copy() for w in T0w]
        for a, b in CHAINS:
            if a not in idx or b not in idx or a not in St or b not in St:
                continue
            ia, ib = idx[a], idx[b]
            cur = Wt[ib][:3, 3] - Wt[ia][:3, 3]
            want = Bt @ (Bs_t.T @ (St[b] - St[a]))
            if stand and ("UpLeg" in a or a.endswith("Leg")):   # standing: legs straight down, feet together (user)
                want = -Bt[:, 1] + Bt[:, 0] * (0.03 if a.startswith("Left") else -0.03)
            elif stand and a.endswith("Foot"):
                want = Bt[:, 2] - Bt[:, 1] * 0.55
            cur, want = cur / np.linalg.norm(cur), want / np.linalg.norm(want)
            ax = np.cross(cur, want)
            sn, cs = np.linalg.norm(ax), float(np.dot(cur, want))
            if sn < 1e-8:
                continue
            D = np.eye(4)
            D[:3, :3] = rot(ax / sn, np.arctan2(sn, cs))
            piv = Wt[ia][:3, 3].copy()
            D[:3, 3] = piv - D[:3, :3] @ piv
            for k in subtree(ia):
                Wt[k] = D @ Wt[k]
        rise = 0.0 if stand else (St["Hips"] - S0["Hips"]) @ Bs[:, 1] * legs[1] / legs[0]
        for w in Wt:
            w[:3, 3] += Bt[:, 1] * rise
        v, tri = pose(tj, tacc, None, 0.0, world_in=Wt)
        out.append(v)
    return out, tri


def generated_walk(j, acc, frames):
    """A walk cycle for a still model with a DAZ-style skeleton (lThighBend / lShin …, the 오블렛 person): thighs swing
    ±24° front / back in opposition, knees bend up to ~55° while the leg swings forward, the body bobs a little; arms
    stay as posed. Returns (idle vertices, [walk vertices …], triangles), glTF coordinates."""
    lt, rt_ = joint_pos(j, acc, "lThighBend"), joint_pos(j, acc, "rThighBend")
    lat = lt - rt_
    lat[1] = 0
    lat /= np.linalg.norm(lat)                       # towards the person's left
    fwd = np.cross(lat, [0, 1, 0])                   # left × up = forward
    toe = joint_pos(j, acc, "lToe") - joint_pos(j, acc, "lFoot")
    if np.dot(toe[[0, 2]], fwd[[0, 2]]) < 0:          # toes point the other way: flip
        lat, fwd = -lat, -fwd
    idle, tri = pose(j, acc, None, 0.0)
    out = []
    for k in range(frames):
        ph = 2 * np.pi * k / frames
        bends = []
        for side, sgn in (("l", 1), ("r", -1)):
            sw = np.radians(24) * np.sin(ph) * sgn                       # + = thigh forward
            vel = np.cos(ph) * sgn                                       # > 0 while this leg swings forward
            knee = np.radians(6 + 52 * max(0.0, vel) ** 1.4)
            bends += [(f"{side}ThighBend", lat, -sw), (f"{side}Shin", lat, knee)]
        v, _ = pose(j, acc, None, 0.0, bends)
        v[:, 1] += 0.012 * (np.abs(np.cos(ph)) - 0.5)                   # small bob (m)
        out.append(v)
    return idle, out, tri, fwd


def quat_mat(q):
    x, y, z, w = q / (np.linalg.norm(q) or 1)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def trs(t, r, s):
    M = np.eye(4)
    M[:3, :3] = quat_mat(np.asarray(r, float)) * np.asarray(s, float)
    M[:3, 3] = t
    return M


def sample(times, values, t, path, interp):
    if interp == "CUBICSPLINE":
        values = values.reshape(len(times), 3, -1)[:, 1]
    if t <= times[0]:
        return values[0]
    if t >= times[-1]:
        return values[-1]
    i = int(np.searchsorted(times, t) - 1)
    if interp == "STEP":
        return values[i]
    u = (t - times[i]) / (times[i + 1] - times[i])
    a, b = values[i].astype(float), values[i + 1].astype(float)
    if path == "rotation":
        if np.dot(a, b) < 0:
            b = -b
        q = a * (1 - u) + b * u
        return q / np.linalg.norm(q)
    return a * (1 - u) + b * u


LAST = {}
DROP = []


def qmul(a, b):   # quaternions (x, y, z, w)
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz])


def qinv(q):
    return np.array([-q[0], -q[1], -q[2], q[3]]) / np.dot(q, q)


def resolve(j, acc, clip, target=None, strip=""):
    """A clip's channels as (node index, path, times, values, interpolation). With target (another model's glTF json),
    rotations are moved over by bone name (minus the prefix strip, e.g. "mixamorig:"): the Mixamo-named clip of one
    model drives a model with the same bone names (Ready Player Me). Translations / scales are not moved (bone lengths
    differ)."""
    names = {n.get("name"): i for i, n in enumerate((target or j)["nodes"])}
    rest = lambda n: np.asarray(n.get("rotation", [0, 0, 0, 1]), float)
    out = []
    for ch in clip["channels"]:
        sm, tg = clip["samplers"][ch["sampler"]], ch["target"]
        if "node" not in tg:
            continue
        node = tg["node"]
        if target is not None:
            if tg["path"] != "rotation":
                continue
            src = j["nodes"][node]
            node = names.get(src.get("name", "").replace(strip, ""))
            if node is None:
                continue
            # the change from the source bone's rest, applied on the target bone's rest (rigs whose rests or parent
            # frames differ, e.g. an armature turned to Z-up, otherwise come out lying down)
            qs_inv, qt = qinv(rest(src)), rest(target["nodes"][node])
            vals = acc(sm["output"]).astype(float)
            if sm.get("interpolation") == "CUBICSPLINE":
                vals = vals.reshape(-1, 3, 4)[:, 1]
            vals = np.array([qmul(qt, qmul(qs_inv, q)) for q in vals])
            out.append((node, "rotation", acc(sm["input"]).reshape(-1), vals, "LINEAR"))
            continue
        out.append((node, tg["path"], acc(sm["input"]).reshape(-1), acc(sm["output"]), sm.get("interpolation", "LINEAR")))
    return out


def pose(j, acc, clip, t, bends=None, world_in=None):
    """World positions of every mesh vertex (one array) at time t of clip (None = rest). bends: [(joint name, axis
    (world, unit), angle rad)] applied root → leaf as rotations about the joint's current world position, carrying the
    joint's whole subtree (a generated walk for a model without clips)."""
    nodes = j["nodes"]
    local = []
    for n in nodes:
        if "matrix" in n:
            local.append(["m", np.array(n["matrix"], float).reshape(4, 4).T])
        else:
            local.append([n.get("translation", [0, 0, 0]), n.get("rotation", [0, 0, 0, 1]), n.get("scale", [1, 1, 1])])
    if clip is not None:   # clip = resolve()d channels: (node, path, times, values, interpolation)
        for node, path, times, values, interp in clip:
            if isinstance(local[node][0], str):
                continue
            k = {"translation": 0, "rotation": 1, "scale": 2}.get(path)
            if k is not None:
                local[node][k] = sample(times, values, t, path, interp)
    parent = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}
    world = [None] * len(nodes)

    def Wm(i):
        if world[i] is None:
            L = local[i][1] if isinstance(local[i][0], str) else trs(*local[i])
            world[i] = (Wm(parent[i]) if i in parent else np.eye(4)) @ L
        return world[i]
    if bends:
        for i in range(len(nodes)):
            Wm(i)
        kids = {i: n.get("children", []) for i, n in enumerate(nodes)}
        by_name = {n.get("name"): i for i, n in enumerate(nodes)}

        def subtree(i):
            out = [i]
            for c in kids[i]:
                out += subtree(c)
            return out
        for name, axis, ang in bends:
            i = by_name[name]
            piv = world[i][:3, 3].copy()
            D = np.eye(4)
            D[:3, :3] = rot(np.asarray(axis, float), ang)
            D[:3, 3] = piv - D[:3, :3] @ piv
            for k in subtree(i):
                world[k] = D @ world[k]
    if world_in is not None:
        world[:] = world_in
    LAST.clear()
    LAST.update({n.get("name"): Wm(i) for i, n in enumerate(nodes) if n.get("name")})   # joint world matrices of this pose
    LAST["__world__"] = list(world)
    out, tris, base = [], [], 0
    for ni, n in enumerate(nodes):
        if "mesh" not in n:
            continue
        skin = j["skins"][n["skin"]] if "skin" in n else None
        if skin:
            ibm = acc(skin["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)
            jm = np.array([Wm(jn) @ ibm[k] for k, jn in enumerate(skin["joints"])])
        for p in j["meshes"][n["mesh"]]["primitives"]:
            mat = j["materials"][p["material"]].get("name", "") if "material" in p and "materials" in j else ""
            if any(d and d in mat for d in DROP):   # parts left out (--drop), e.g. the hat / beard
                continue
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
                v = (Wm(ni) @ ph.T).T
            idx = (acc(p["indices"]).reshape(-1) if "indices" in p else np.arange(len(pos))).astype(np.int64)
            out.append(v[:, :3])
            tris.append(idx.reshape(-1, 3) + base)
            base += len(pos)
    return np.concatenate(out), np.concatenate(tris)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("glb"), ap.add_argument("out")
    ap.add_argument("--walk", default="Walk"), ap.add_argument("--idle", default="Idle")
    ap.add_argument("--frames", type=int, default=16), ap.add_argument("--turn", type=float, default=0.0)
    ap.add_argument("--generated", action="store_true", help="no clips: generate the walk from the skeleton (오블렛 person)")
    ap.add_argument("--cell", type=float, default=0.0, help="simplify by vertex clustering (mm, shared by all frames)")
    ap.add_argument("--clips-from", default="", help="take the Idle / Walk clips from this glb (bones matched by name)")
    ap.add_argument("--strip", default="mixamorig:", help="bone name prefix removed from the clip's bones")
    ap.add_argument("--auto-face", action="store_true", help="turn so the toes point to +y on the plan")
    ap.add_argument("--drop", default="", help="leave out mesh parts whose material name contains these (comma separated)")
    a = ap.parse_args()
    DROP[:] = [d for d in a.drop.split(",") if d]
    j, acc = load(a.glb)
    if a.generated:
        idle_v, walk_v, tri, fwd = generated_walk(j, acc, a.frames)
        frames = [idle_v] + walk_v
        a.turn = float(np.degrees(np.arctan2(-fwd[0], fwd[2]))) if not a.turn else a.turn   # make the person face +z (plan +y)
        dur = 0.0
        return write(a, frames, tri, f"generated walk, {a.frames} poses")
    src_j, src_acc = load(a.clips_from) if a.clips_from else (j, acc)
    anims = src_j.get("animations", [])
    clips = {c.get("name"): c for c in anims}
    walk, idle = clips.get(a.walk) or anims[0], clips.get(a.idle)   # unnamed single clip: the walk; no idle clip: the rest pose
    dur = max(float(src_acc(s["input"]).max()) for s in walk["samplers"])
    if a.clips_from:   # another rig: follow its bone directions
        rw, ri = resolve(src_j, src_acc, walk), resolve(src_j, src_acc, idle) if idle else None
        frames, tri = retarget_dirs(j, acc, src_j, src_acc, rw, [dur * k / a.frames for k in range(a.frames)], a.strip)
        if ri is not None:   # the standing pose: the idle clip's upper body, legs straight and together
            fi, tri = retarget_dirs(j, acc, src_j, src_acc, ri, [0.0], a.strip, stand=True)
            frames.insert(0, fi[0])
        else:
            frames.insert(0, min(frames, key=lambda f: np.ptp(f[:, 2])))
        if a.auto_face:   # the body frame is forward = left × up already: +z in glTF after body_basis is the facing
            a.turn = 0.0
            pose(j, acc, None, 0.0)
            T0 = {k: v[:3, 3] for k, v in LAST.items() if k != "__world__"}
            fwd = body_basis(T0)[:, 2]
            a.turn = float(np.degrees(np.arctan2(-fwd[0], fwd[2])))
        return write(a, frames, tri, f"{a.frames} walk poses over {dur:.2f} s (bone directions from {Path(a.clips_from).name})")
    tgt = None
    rw = resolve(src_j, src_acc, walk, tgt, a.strip)
    ri = resolve(src_j, src_acc, idle, tgt, a.strip) if idle else None
    frames = []
    for k in range(a.frames):
        v, tri = pose(j, acc, rw, dur * k / a.frames)
        frames.append(v)
    if ri is not None:
        v, tri = pose(j, acc, ri, 0.0)
        frames.insert(0, v)
    else:   # no idle clip (the rest pose may be a T-pose): stand in the walk pose with the feet closest together
        frames.insert(0, min(frames, key=lambda f: np.ptp(f[:, 2])))
    if a.auto_face:   # face +z (plan +y): from the left foot to its toes in the standing pose
        pose(j, acc, ri, 0.0)
        foot = next(k for k in LAST if k.endswith("LeftFoot")); toe = next(k for k in LAST if k.endswith("LeftToeBase"))
        d = LAST[toe][:3, 3] - LAST[foot][:3, 3]
        a.turn = float(np.degrees(np.arctan2(-d[0], d[2])))
    return write(a, frames, tri, f"{a.frames} walk poses over {dur:.2f} s")


def write(a, frames, tri, what):
    th = np.radians(a.turn)
    R = np.array([[np.cos(th), 0, np.sin(th)], [0, 1, 0], [-np.sin(th), 0, np.cos(th)]])
    frames = [f @ R.T for f in frames]
    f0 = frames[0]
    lo, hi = f0[:, 1].min(), f0[:, 1].max()
    s = HEIGHT_MM / (hi - lo)
    cx, cz = (f0[:, 0].min() + f0[:, 0].max()) / 2, (f0[:, 2].min() + f0[:, 2].max()) / 2
    pls = [np.stack([f[:, 0] - cx, f[:, 2] - cz, f[:, 1] - lo], 1) * s for f in frames]   # glTF (x, y up, z forward) → plan (x, y forward, z up)
    if a.cell:   # one clustering (on the standing pose) for every frame, so all frames keep the same triangles
        q = np.round(pls[0] / a.cell).astype(np.int64)
        _, inv = np.unique(q, axis=0, return_inverse=True)
        inv = inv.reshape(-1)
        cnt = np.bincount(inv)
        merged = []
        for pl in pls:
            c = np.zeros((len(cnt), 3))
            np.add.at(c, inv, pl)
            merged.append(c / cnt[:, None])
        t2 = inv[tri]
        t2 = t2[(t2[:, 0] != t2[:, 1]) & (t2[:, 1] != t2[:, 2]) & (t2[:, 0] != t2[:, 2])]
        t2 = t2[np.sort(np.unique(np.sort(t2, 1), axis=0, return_index=True)[1])]
        pls, tri = merged, t2
    recs = []
    for pl in pls:
        a3, b3, c3 = pl[tri[:, 0]], pl[tri[:, 1]], pl[tri[:, 2]]
        fn = np.cross(b3 - a3, c3 - a3)
        fn /= np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-9)
        vn = np.zeros_like(pl)
        for kk in range(3):
            np.add.at(vn, tri[:, kk], fn)
        vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-9)
        rec = np.zeros(len(tri) * 3, dtype=[("p", "<i2", 3), ("n", "i1", 3), ("pad", "i1")])
        rec["p"] = np.round(pl[tri.reshape(-1)]).astype(np.int16)
        rec["n"] = np.round(vn[tri.reshape(-1)] * 127).astype(np.int8)
        recs.append(rec.tobytes())
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_bytes(b"RPA1" + struct.pack("<IIf", len(tri), len(frames), STRIDE_MM) + b"".join(recs))
    print(f"{len(tri)} triangles × {len(frames)} frames (idle + {what}), height {HEIGHT_MM:.0f} mm, "
          f"{Path(a.out).stat().st_size} bytes")


if __name__ == "__main__":
    main()
