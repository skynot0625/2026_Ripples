#!/usr/bin/env python3
"""Static web snapshot of /venue3d (2026-11-06, user: "전시 시뮬레이터 부분만 떼서 민바에 붙여서 배포").

    python tools/export_static_venue3d.py <out_dir> [server_url]     # server_url default http://127.0.0.1:8000

Reads (GET only) /api/venue and /api/simulator from a running server and writes a folder that works without it:
index.html (venue3d.html, server paths made relative, full UI always on), venue.json, state.json (+ the default
layout), person_mesh.bin and static-sim.js (simulator.js with connect() replaced by an in-browser state: layout edits,
dragging, driving and reset work locally; nothing is saved except through 배치 저장 / 배치 불러오기 files; no ToF data,
no collision; people walk, ride 혈 / 맥 in 수집 and the 관람 시뮬 visitor can get on / off / take over: tools/static_sim.js).

The same files as one zip, with 읽어주세요.txt for whoever puts it on a web site: GET /api/venue3d-static, the
「웹판 내려받기」 button in /venue3d (user 2026-11-09: "ui 안에서 클릭해서 다운받는구조", "연결할 작업자에게 안내하는 글").
"""
import hashlib
import io
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STATIC_CONNECT = (ROOT / "tools" / "static_sim.js").read_text(encoding="utf-8")   # the in-page state + people / sessions (2026-11-07)

GUIDE = """리플즈 M30 공간 시뮬레이터 웹판 ({made})

이 폴더는 서버 없이 웹에서 열리는 3D 공간 시뮬레이터입니다.
전시 서버 화면(/venue3d)을 내려받은 그때의 배치 그대로 구웠습니다.
공간: {venue} · 구조물 {solids}개


1. 올리는 법

- 폴더 안 파일을 모두 같은 폴더에 그대로 올립니다. 파일 이름과 위치를 바꾸지 않습니다.
- 정적 파일만 있으면 됩니다. 서버 프로그램, 데이터베이스, 빌드 과정이 필요 없습니다.
  (Vercel public 폴더, Netlify, GitHub Pages, 일반 웹호스팅 모두 됩니다.)
- 주소는 index.html까지 붙여서 엽니다.
  예: https://example.com/ripples-m30/index.html
  폴더 주소(/ripples-m30/)만으로는 열리지 않는 호스팅이 있습니다.


2. 다른 페이지에 붙이는 법

- 링크: <a href="/ripples-m30/index.html">공간 시뮬레이터</a>
- 페이지 안에 넣기:
  <iframe src="/ripples-m30/index.html" style="width:100%;height:80vh;border:0" allowfullscreen></iframe>
- 키보드 조작(WASD, F)은 iframe을 한 번 클릭해야 들어갑니다.


3. 확인하는 법

- index.html을 더블클릭하면 열리지 않습니다(브라우저가 파일 주소에서 venue.json 읽기를 막습니다).
- 내 컴퓨터에서 보려면 이 폴더에서 다음을 실행한 뒤 http://localhost:8080/index.html 을 엽니다.
  python3 -m http.server 8080
- 화면이 비어 있으면 브라우저 개발자 도구 콘솔에서 404가 난 파일을 확인합니다.


4. 파일

- index.html        화면
- static-sim.js     시뮬레이터 (브라우저 안에서 돎)
- venue.json        공간 도면
- state.json        내려받은 때의 배치와 장치 위치
- person_mesh.bin, person_anim.bin   사람 모델과 걷기 동작
- ripples-example.js/json, ripples-activate.rgb.gz, ripples-sleep.rgb.gz   심탑 감각 예시
- tower-audio/      심탑 소리 뱅크·시간표·스테레오 미리듣기 (소리 시작 버튼)
- 읽어주세요.txt     이 글 (올리지 않아도 됩니다)


5. 되는 것과 안 되는 것

- 됩니다: 시점 바꾸기, 배치 편집, 끌기, 혈·맥 조종, 관람 시뮬(걷기, 타기, 뺏기), 관객 걷기.
- 바뀐 배치는 저장되지 않습니다. 새로고침하면 처음 배치로 돌아갑니다.
  남기려면 화면의 「배치 저장」으로 파일을 받아 두고 「배치 불러오기」로 엽니다.
- 실제 센서(ToF) 데이터와 충돌 계산은 들어 있지 않습니다.
- 소리는 자동 재생하지 않습니다. 소리 시작을 누르면 LED의 Activate/Sleep과 연동합니다.
  브라우저는 스테레오 미리듣기이며, Sleep 원음은 합성 예시입니다. 실제 4출력 장치는 별도입니다.
- 배치를 바꿔서 다시 올리려면 전시 서버 /venue3d 화면의 「웹판 내려받기」로 새로 받아 파일을 모두 바꿉니다.
  (static-sim.js 주소에 버전이 붙어 있어 방문자 브라우저도 새 파일을 받습니다.)
"""


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def build(venue, state, default_layout):
    """venue = /api/venue, state = /api/simulator (both left untouched). Returns {file name: bytes}."""
    state = json.loads(json.dumps(state))
    state["default_layout"] = default_layout
    for k in ("origin", "seq", "path", "save_error", "saved"):
        state.pop(k, None)
    for p in state.get("people", []):   # the visitors walk in the page (tools/static_sim.js); their ToF parts are not needed
        p.pop("parts", None)
    shim = (ROOT / "simulator.js").read_text(encoding="utf-8") + STATIC_CONNECT
    ver = hashlib.sha256(shim.encode("utf-8")).hexdigest()[:10]   # in the script's address: a browser never keeps an old copy (user 2026-11-07)
    html = (ROOT / "venue3d.html").read_text(encoding="utf-8")
    for a, b in (('<script src="/simulator.js"></script>', f'<script src="static-sim.js?v={ver}"></script>'),
                 ('<script src="/static/ripples-example.js"></script>', '<script src="ripples-example.js"></script>'),
                 ('<script src="/static/tower-audio/player.js"></script>', '<script src="tower-audio/player.js"></script>'),
                 ('<script src="/static/tower-audio/speaker-panel.js"></script>', '<script src="tower-audio/speaker-panel.js"></script>'),
                 ('  <script src="/testui.js"></script>\n', ''),
                 ("fetch('/api/venue', {cache: 'no-store'})", "fetch('venue.json', {cache: 'no-store'})"),
                 ("fetch('/static/person_mesh.bin'", "fetch('person_mesh.bin'"),
                 ("fetch('/static/person_anim.bin'", "fetch('person_anim.bin'"),
                 ('<a id="webexport" class="btn" href="/api/venue3d-static" download>웹판 내려받기</a>', ''),
                 ("document.body.classList.toggle('plain', !query.has('test'));", "document.body.classList.toggle('plain', false);   // static: full UI")):
        assert html.count(a) == 1, a
        html = html.replace(a, b)
    files = {"index.html": html.encode("utf-8"),
             "venue.json": json.dumps(venue, ensure_ascii=False).encode("utf-8"),
             "state.json": json.dumps(state, ensure_ascii=False).encode("utf-8"),
             "static-sim.js": shim.encode("utf-8")}
    for f in ("person_mesh.bin", "person_anim.bin"):
        files[f] = (ROOT / "static" / f).read_bytes()
    for f in ("ripples-example.json", "ripples-activate.rgb.gz", "ripples-sleep.rgb.gz"):
        files[f] = (ROOT / "static" / f).read_bytes()
    files["ripples-example.js"] = (ROOT / "static" / "ripples-example.js").read_text(encoding="utf-8").replace(
        "const root = '/static/'", "const root = './'"
    ).encode("utf-8")
    for path in (ROOT / "static" / "tower-audio").rglob("*"):
        if path.is_file() and path.suffix in (".json", ".js", ".wav"):
            files["tower-audio/" + path.relative_to(ROOT / "static" / "tower-audio").as_posix()] = path.read_bytes()
    files["읽어주세요.txt"] = GUIDE.format(made=time.strftime("%Y-%m-%d %H:%M"), venue=venue.get("id"),
                                       solids=len(venue.get("solids", []))).encode("utf-8")
    return files


def zip_bytes(files, folder):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(f"{folder}/{name}", data)
    return buf.getvalue()


def main(out, server="http://127.0.0.1:8000"):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    venue, state = get(server + "/api/venue"), get(server + "/api/simulator")
    import venue as V
    files = build(venue, state, V.load_venue(venue["id"]).default_layout())
    for name, data in files.items():
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(data)
    print(f"{out}: {', '.join(files)} ({len(venue['solids'])} solids)")


if __name__ == "__main__":
    main(*sys.argv[1:])
