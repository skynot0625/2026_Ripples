#!/usr/bin/env python3
"""Start / stop the exhibition: server.py plus one borderless browser window per output (booth).

    python exhibition.py start [--config exhibition.json] [--dry-run] [--no-browser]
    python exhibition.py stop
    python exhibition.py status

Everything stays inside this folder: settings in exhibition.json, process ids and logs in run/, one browser profile
per output in run/browser-<name>/ (no personal account, no administrator rights). Monitor position and size are
settings (outputs[].window), filled in on installation day. Standard library only; works on Windows, macOS and Linux.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN = ROOT / "run"
STATE = RUN / "processes.json"
WINDOWS = os.name == "nt"


def browser_candidates() -> list[str]:
    if WINDOWS:
        env = os.environ
        roots = [env.get("ProgramFiles(x86)"), env.get("ProgramFiles"), env.get("LOCALAPPDATA")]
        rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
        return [str(Path(r) / p) for p in rel for r in roots if r]
    if sys.platform == "darwin":
        return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    return [p for p in (shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge")) if p]


def find_browser(setting: str) -> str | None:
    if setting and setting != "auto":
        return setting if Path(setting).exists() else None
    return next((p for p in browser_candidates() if Path(p).exists()), None)


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    port = int(cfg["server"]["port"])
    if not 1 <= port <= 65535:
        raise ValueError("server.port must be 1–65535")
    for out in cfg["outputs"]:
        if not str(out["url"]).startswith("/"):
            raise ValueError(f"{out['name']}: url must start with /")
        for key in ("x", "y", "width", "height"):
            v = out.get("window", {}).get(key)
            if v is not None and not isinstance(v, int):
                raise ValueError(f"{out['name']}: window.{key} must be a whole number or null")
    return cfg


def detach_kwargs(breakaway: bool = True) -> dict:
    # The processes keep running after this script (and its console) ends. On Windows an SSH session runs in a job
    # object that is closed (and its processes killed) when the session ends, so the processes also leave that job.
    if WINDOWS:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        return {"creationflags": flags | (subprocess.CREATE_BREAKAWAY_FROM_JOB if breakaway else 0)}
    return {"start_new_session": True}


def spawn(cmd: list[str], **kwargs) -> subprocess.Popen:
    try:
        return subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, **kwargs, **detach_kwargs())
    except PermissionError:  # the job does not allow breakaway: start normally (it then ends with the session)
        print("경고: 현재 세션 밖으로 프로세스를 분리할 수 없어, 이 창(세션)을 닫으면 함께 종료될 수 있습니다.")
        return subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, **kwargs, **detach_kwargs(breakaway=False))


def server_command(cfg: dict) -> list[str]:
    s = cfg["server"]
    cmd = [sys.executable, str(ROOT / "server.py"), "--port", str(s["port"]), "--venue", s.get("venue", "testroom"),
           "--mapping-file", s.get("mapping_file", "data/mapping.json"), "--simulator-file", s.get("simulator_file", "data/simulator.json")]
    if s.get("legacy_mapping_file"):
        cmd += ["--legacy-mapping-file", s["legacy_mapping_file"]]
    cmd += [str(a) for a in s.get("extra_args", [])]   # e.g. 혈 Pi 실시간: --source collector --distance collector
    return cmd + (["--mock"] if s.get("mock") else [])


def browser_command(browser: str, cfg: dict, out: dict) -> list[str]:
    url = f"http://127.0.0.1:{cfg['server']['port']}{out['url']}"
    w = out.get("window", {})
    cmd = [browser, f"--user-data-dir={RUN / ('browser-' + out['name'])}", "--no-first-run", "--no-default-browser-check",
           "--disable-session-crashed-bubble", "--noerrdialogs", "--disable-infobars", "--disable-translate"]
    if w.get("x") is not None and w.get("y") is not None:
        cmd.append(f"--window-position={w['x']},{w['y']}")
    if w.get("width") and w.get("height"):
        cmd.append(f"--window-size={w['width']},{w['height']}")
    return cmd + (["--kiosk", url] if cfg["browser"].get("kiosk", True) else [f"--app={url}"])


def alive(pid: int) -> bool:
    if WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def server_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def start(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    port = cfg["server"]["port"]
    state = read_state()
    if any(alive(p["pid"]) for p in state.get("processes", [])):
        print("이미 실행 중입니다. 먼저 stop을 실행하세요.")
        return 1
    cmds = [("server", server_command(cfg))]
    missing_window = []
    if not args.no_browser:
        browser = find_browser(cfg["browser"].get("path", "auto"))
        if not browser:
            print("브라우저(Edge 또는 Chrome)를 찾지 못했습니다. exhibition.json의 browser.path에 실행 파일 경로를 적으세요.")
            return 1
        for out in cfg["outputs"]:
            if out.get("enabled", True):
                cmds.append((out["name"], browser_command(browser, cfg, out)))
                if any(out.get("window", {}).get(k) is None for k in ("x", "y", "width", "height")):
                    missing_window.append(out["name"])
    for name in missing_window:
        print(f"[{name}] window 위치·크기가 비어 있음 → 브라우저가 정한 위치에 엽니다 (설치 당일 exhibition.json에 입력).")
    if args.dry_run:
        for name, cmd in cmds:
            print(f"{name}: {subprocess.list2cmdline(cmd) if WINDOWS else ' '.join(cmd)}")
        return 0
    RUN.mkdir(exist_ok=True)
    if server_ready(port):
        print(f"port {port}에 이미 다른 서버가 응답합니다. 끄거나 exhibition.json의 server.port를 바꾸세요.")
        return 1
    processes = []
    log = open(RUN / "server.log", "a", encoding="utf-8")
    env = dict(os.environ, **{k: str(v) for k, v in cfg["server"].get("env", {}).items()})   # e.g. COLLECTOR_LISTEN=9500
    server = spawn(cmds[0][1], stdout=log, stderr=subprocess.STDOUT, env=env)
    processes.append({"name": "server", "pid": server.pid})
    deadline = time.monotonic() + 30
    while not server_ready(port):
        if server.poll() is not None or time.monotonic() > deadline:
            print(f"서버가 시작되지 않았습니다. {RUN / 'server.log'}를 확인하세요.")
            if server.poll() is None:
                server.terminate()
            return 1
        time.sleep(0.3)
    print(f"서버 실행: http://127.0.0.1:{port}  (휴대폰: http://<이 컴퓨터 LAN IP>:{port}/mapping?test)")
    for name, cmd in cmds[1:]:
        p = spawn(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append({"name": name, "pid": p.pid})
        print(f"출력 창 실행: {name}")
    STATE.write_text(json.dumps({"started": time.strftime("%Y-%m-%d %H:%M:%S"), "port": port, "processes": processes}, indent=2), encoding="utf-8")
    return 0


def kill(pid: int, graceful: bool) -> None:
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(pid, signal.SIGINT if graceful else signal.SIGTERM)
    except OSError:
        pass


def stop(args: argparse.Namespace) -> int:
    state = read_state()
    procs = state.get("processes", [])
    if not procs:
        print("실행 기록이 없습니다 (run/processes.json).")
        return 0
    for p in procs:  # browsers first, so no more changes arrive
        if p["name"] != "server" and alive(p["pid"]):
            kill(p["pid"], graceful=False)
            print(f"출력 창 종료: {p['name']}")
    time.sleep(1.0)  # let the server finish its debounced mapping write (0.5 s)
    for p in procs:
        if p["name"] == "server" and alive(p["pid"]):
            kill(p["pid"], graceful=True)  # macOS/Linux: Ctrl+C equivalent, flushes mapping; Windows: taskkill
            for _ in range(30):
                if not alive(p["pid"]):
                    break
                time.sleep(0.2)
            print("서버 종료")
    STATE.unlink(missing_ok=True)
    return 0


def status(args: argparse.Namespace) -> int:
    state = read_state()
    if not state:
        print("실행 기록 없음")
        return 0
    print(f"시작 {state.get('started')} · port {state.get('port')} · 서버 응답 {'있음' if server_ready(state.get('port', 8000)) else '없음'}")
    for p in state.get("processes", []):
        print(f"  {p['name']:10} pid {p['pid']:>7}  {'실행 중' if alive(p['pid']) else '종료됨'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="전시 시작·중지 (server.py + 출력 창)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start")
    p_start.add_argument("--config", default=str(ROOT / "exhibition.json"))
    p_start.add_argument("--dry-run", action="store_true", help="실행할 명령만 출력")
    p_start.add_argument("--no-browser", action="store_true", help="서버만 실행")
    sub.add_parser("stop")
    sub.add_parser("status")
    args = parser.parse_args()
    return {"start": start, "stop": stop, "status": status}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
