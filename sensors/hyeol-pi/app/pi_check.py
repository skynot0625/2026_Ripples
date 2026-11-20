#!/usr/bin/env python3
"""Pi 실행본 확인: 설정 확인값 (manifest = visual.json), 라이브러리, (--camera) Arducam 5초 측정, (--distance) 천장 거리
센서, (--running) 켜져 있는 서버에 카메라 · 센서 상태를 물어봄. 서버가 켜져 있을 때 --camera / --distance 는 쓰지 않는다
(카메라 · 직렬 포트는 한 프로그램만 쓴다: check.sh 가 알아서 고름)."""
import argparse, json, sys, time, urllib.request
from pathlib import Path

here = Path(__file__).resolve().parent
root = here.parent
sys.path.insert(0, str(here))
ap = argparse.ArgumentParser()
ap.add_argument("--camera", action="store_true")
ap.add_argument("--camera-index", type=int, default=0)
ap.add_argument("--distance", default="", help="천장 거리 센서 포트 (auto / 경로); 비우면 확인 안 함")
ap.add_argument("--distance-baud", type=int, default=0)
ap.add_argument("--running", action="store_true", help="켜져 있는 서버(127.0.0.1:8000)에 물어봄")
ap.add_argument("--quiet", action="store_true", help="--running 의 입력 두 줄만")
a = ap.parse_args()
ok = True


def running() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/status", timeout=3) as r:
            st = json.loads(r.read())
        with urllib.request.urlopen("http://127.0.0.1:8000/inputs?format=json", timeout=3) as r:
            raw = r.read()
        try:
            ch = json.loads(raw).get("channels", {}).get("distance", {})
        except ValueError:
            ch = {}
    except Exception as exc:
        print(f"서버에 닿지 않음: {exc}"); return False
    cam = f"ToF 카메라: {st.get('resolution')} · {st.get('fps')} fps" if st.get("connected") else f"ToF 카메라: 안 열림 · {st.get('error')}"
    print(cam)
    if ch.get("connected") and isinstance(ch.get("value"), (int, float)):
        print(f"천장 거리 센서: 지금 {ch['value']:.2f} m · {ch.get('rate_hz')} Hz")
    elif ch.get("connected"):
        print("천장 거리 센서: 연결됨 · 측정값 없음 (위가 너무 멀거나 가까움)")
    else:
        print(f"천장 거리 센서: 값 없음 · {ch.get('error') or '연결 안 됨'} (run/server.log)")
    return bool(st.get("connected"))


if a.running:
    sys.exit(0 if running() else 1)

m = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
import pi_bundle
from visual_store import BUILTINS, VisualStore
v = json.loads((root / "data" / "visual.json").read_text(encoding="utf-8"))
cid = v["slots"]["hyeol_blind"]
store = VisualStore.__new__(VisualStore)
comp = dict(BUILTINS[cid]) if cid in BUILTINS else VisualStore._parse(store, v)["compositions"][cid]
comp["id"] = cid
h = pi_bundle.composition_hash(comp)
print(f"화면 {m['name']} ({m['id']}) · revision {m['revision']} · 확인값 {m['hash']}")
if h != m["hash"] or cid != m["id"]:
    print(f"설정 불일치: data/visual.json 확인값 {h} (manifest {m['hash']})"); ok = False
else:
    print("설정 일치")
for name in ("numpy", "cv2", "ArducamDepthCamera"):
    try:
        __import__(name); print(f"{name} 있음")
    except Exception as exc:
        print(f"{name} 없음: {exc}"); ok = False
if a.camera and ok:
    import ArducamDepthCamera as ac
    cam = ac.ArducamCamera()
    r = cam.open(ac.Connection.CSI, a.camera_index)
    if r != 0:
        print(f"카메라 열기 실패 ({r}) · index {a.camera_index} · 리본 케이블 · CAM/DISP 0 · 카메라 전원, settings.sh 의 CAMERA_INDEX=8"); ok = False
    else:
        try:
            if cam.start(ac.FrameType.DEPTH) != 0:
                print("카메라 시작 실패"); ok = False
            else:
                info = cam.getCameraInfo()
                n, t0 = 0, time.monotonic()
                while time.monotonic() - t0 < 5:
                    f = cam.requestFrame(2000)
                    if f is not None:
                        n += 1; cam.releaseFrame(f)
                print(f"카메라 {info.width}x{info.height} · 5초 동안 {n}프레임 · 수집 {n / 5:.1f} fps (표시 fps는 화면 진단에서 따로 확인)")
                cam.stop()
        finally:
            cam.close()
if a.distance:
    import tofsense
    found = tofsense.find(a.distance, a.distance_baud or None, seconds=1.5)
    if found is None:
        ports = tofsense.candidate_ports(a.distance)
        if not ports:
            print("천장 거리 센서: 포트 없음 · install.sh 뒤 재부팅했는지, 또는 USB-UART 어댑터를 꽂았는지");
        else:
            print(f"천장 거리 센서: 신호 없음 (찾아본 포트 {', '.join(ports)}) · TX↔RX 엇갈림 · 5V · GND · 센서가 UART 모드인지 (README 1. 연결)")
        ok = False
    else:
        import os, select
        path, baud, fd, frames = found
        parser, vals, t0 = tofsense.FrameParser(), [f["m"] for f in frames], time.monotonic()
        while time.monotonic() - t0 < 2:
            if select.select([fd], [], [], 0.2)[0]:
                vals += [f["m"] for f in parser.feed(os.read(fd, 1024))]
        os.close(fd)
        good = [x for x in vals if x is not None]
        rate = len(vals) / (time.monotonic() - t0 + 1e-6)
        now = f"지금 {good[-1]:.2f} m" if good else "측정값 없음 (위가 너무 멀거나 가까움)"
        print(f"천장 거리 센서: {path} · {baud} baud · {now} · 약 {rate:.0f} Hz · 유효 {len(good)}/{len(vals)}")
for layer in m["layers"]:
    if layer["status"] in ("missing", "pose"):
        print(f"주의 · {layer['name']}: " + " / ".join(layer["needs"][1:] if layer["needs"][:1] and layer["needs"][0].startswith("ToF") else layer["needs"]))
sys.exit(0 if ok else 1)
