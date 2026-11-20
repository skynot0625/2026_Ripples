#!/bin/bash
# 화면 설정을 바로 바꿈 (다시 띄우지 않음): 켜져 있는 서버에 넣으면 화면이 곧바로 바뀌고, manifest 확인값도 맞춰서
# 다음에 켤 때 「설정 불일치」로 멈추지 않게 함.
#   bash tune.sh '{"tof_spaceline": {"sag_s": 6, "color_far": "#3d8bff"}, "updist_meter": {"auto_span": 0.25, "fx.trail.on": true}}'
#   (레이어 종류별 설정, 효과는 "fx.<효과>.<항목>")
cd "$(dirname "$0")"
PY="$HOME/tof-env/bin/python"; [ -x "$PY" ] || PY=python3
"$PY" - "$1" <<'PY'
import json, sys, urllib.request
sys.path.insert(0, "app"); import pi_bundle
U = "http://127.0.0.1:8000/api/visual"
m = json.load(open("manifest.json")); cid = m["id"]
d = json.load(urllib.request.urlopen(U)); c = d["compositions"][cid]
ch = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else {}
for l in c["layers"]:
    for k, v in ch.get(l["renderer"], {}).items():
        if k.startswith("fx."):
            a, b = k[3:].split("."); l.setdefault("effects", {}).setdefault(a, {})[b] = v
        else:
            l["params"][k] = v
body = {"op": {"op": "set", "id": cid, "composition": {k: c[k] for k in ("name", "canvas", "layers")}}, "final": True}
r = urllib.request.urlopen(urllib.request.Request(U, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}))
s = json.loads(r.read())
comp = dict(s["compositions"][cid], id=cid)
m.update(hash=pi_bundle.composition_hash(comp), revision=s["revision"], note="Pi에서 직접 조정 (tune.sh)")
json.dump(m, open("manifest.json", "w"), ensure_ascii=False, indent=1)
print(f"바꿈 · revision {s['revision']} · 확인값 {m['hash']}")
PY
