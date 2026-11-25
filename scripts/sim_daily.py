"""심 하루 자동 운영 (2026-11-24): 매일 10:30~19:00 KST (10:30~11:00 은 개장 전 시험 한 바퀴: 20분 activate + 10분 sleep).

· LED: ripples.live 시간표 모드(configs/schedule.m30.json — 10:30 기준 20분 activate / 10분 sleep),
  --accumulate 로 위치별 기록을 쌓고 sleep 때 그 기록을 재생. 19:00 에 끝나면 검은 화면.
· 소리: 30분 mp3(앞 20분 activate · 뒤 10분 sleep)를 벽시계(30분 경계 = 10:30 기준)에 맞춰 4채널로.
  play_exhibition_example.py 의 소리 부분과 같은 방식. 상태가 바뀌는 순간(정시 · 30분 = activate, 20분 · 50분 = sleep)
  5초 전환 신호음(ripples.transition_audio)을 더한다 — LED 쪽 5초 빛 전환은 live.py 화면 코드에 이미 있음.
· 보드 확인: COM3 + 일련번호가 맞을 때만 자동으로 연다(사용자 2026-11-24 「그냥 자동화」). 다르면 열지 않고 끝.
· 혈 · 맥이 둘 다 끊겨 있으면 저장 데이터(예제 플레이어: 녹화 클립 · 예제 기록 · 같은 mp3)로 돌고, 하나라도 10초 이어
  들어오면 실시간으로 넘어간다. 둘 다 30초 넘게 끊기면 다시 저장 데이터로(사용자 2026-11-25).
· 멈춤: 그날 다시 켜지지 않게 하려면 runtime/sim-daily.off 를 만든다.
· 작업 스케줄러가 10:30 부터 5분마다 부른다 — 이미 돌고 있으면(잠금) 바로 끝, 꺼져 있으면 다시 켠다(멈춤 복구).

실행: <venv>\\Scripts\\python.exe -X utf8 -u scripts\\sim_daily.py   (작업 스케줄러 ripples_sim_daily)
"""
import msvcrt, os, queue, runpy, sys, threading, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "runtime"
RUN.mkdir(parents=True, exist_ok=True)
KST = timezone(timedelta(hours=9))
START, END = (10, 30), (19, 0)   # 시간표 기준(anchor)은 10:30 — 30분 주기라 10:30 도 activate 시작
PORT, SERIAL = "COM3", "20:6E:F1:32:89:50"
MP3 = Path(os.environ["RIPPLES_MP3_PATH"]) if os.environ.get("RIPPLES_MP3_PATH") else None
DB = RUN / "exhibition-live-estimated.sqlite3"
CUE_GAIN = 1.0      # 상태 전환 5초 신호음(ripples.transition_audio.make_cue) 크기 — mp3 위에 더함. 0 이면 끔
STARTS = {"activate": 0.0, "sleep": 1200.0}   # 30분 주기 안에서 각 상태가 시작하는 초(LED 시간표와 같은 경계)


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def window():
    now = datetime.now(KST)
    s = now.replace(hour=START[0], minute=START[1], second=0, microsecond=0)
    e = now.replace(hour=END[0], minute=END[1], second=0, microsecond=0)
    return now, s, e


def board_ok():
    from serial.tools import list_ports
    return any(p.device == PORT and p.serial_number == SERIAL for p in list_ports.comports())


def audio(deadline, ev):
    import numpy as np, sounddevice as sd, soundfile as sf
    if MP3 is None:
        return log("AUDIO mp3 없음")
    devs = [i for i, d in enumerate(sd.query_devices()) if "Realtek" in d["name"] and d["max_output_channels"] >= 4
            and "WASAPI" in sd.query_hostapis(d["hostapi"])["name"]]
    if not devs:
        return log("AUDIO Realtek 4ch WASAPI 없음 — 소리 없이 진행")
    from ripples.transition_audio import make_cue, DURATION as CUE_S
    cues = {k: make_cue(k, 48000) * CUE_GAIN for k in STARTS} if CUE_GAIN > 0 else {}
    q, done = queue.Queue(64), threading.Event()
    played = [0]

    def produce():
        with sf.SoundFile(MP3) as f:
            played[0] = int((time.time() % 1800) * 48000); f.seek(played[0])
            log("AUDIO 벽시계 위치", round(played[0] / 48000, 1), "초")
            while not done.is_set():
                x = f.read(1024, dtype="float32", always_2d=True)
                if not len(x):
                    f.seek(0); continue
                while not done.is_set():
                    try: q.put(x, timeout=.1); break
                    except queue.Full: pass

    def cb(out, frames, t, status):
        out.fill(0)
        if done.is_set():
            raise sd.CallbackStop
        try: x = q.get_nowait()
        except queue.Empty: return
        ph = ((played[0] + np.arange(len(x))) / 48000) % 1800
        g = np.minimum(1, ph / .5) * np.minimum(1, (1800 - ph) / .5)
        y = np.tile(x * g[:, None], (1, 2))
        for k, c in cues.items():                       # 전환 신호음: LED 5초 빛 전환과 같은 순간
            age = int(round(((ph[0] - STARTS[k]) % 1800) * 48000))
            if age < len(c):
                n = min(len(y), len(c) - age); y[:n] += c[age:age + n]
        out[:len(x)] = np.clip(y, -1, 1)
        played[0] += len(x)

    threading.Thread(target=produce, daemon=True).start()
    with sd.OutputStream(device=devs[0], samplerate=48000, channels=4, dtype="float32", blocksize=1024, latency="high", callback=cb):
        log("AUDIO 시작", sd.query_devices(devs[0])["name"])
        while time.time() < deadline and not ev.is_set() and not (RUN / "sim-audio.stop").exists():
            time.sleep(.5)
    done.set(); log("AUDIO 끝")


def black():
    try:
        import numpy as np
        from ripples.hardware.transport import LEDUSB, SHAPE
        b = LEDUSB(PORT, version="rgb565"); b.send(np.zeros(SHAPE, np.uint8)); b.close(); log("LED 검은 화면")
    except Exception as e:
        log("LED 검은 화면 실패", repr(e))


LIVE_AFTER_S, EXAMPLE_AFTER_S = 10, 30   # 연결이 이만큼 이어지면 실시간으로, 끊김이 이만큼 이어지면 저장 데이터로
PY = sys.executable


def connected():
    """혈(거리 · ToF) · 맥(마이크) 중 하나라도 심 서버에 들어오고 있으면 True."""
    import json, urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/inputs", timeout=2) as r:
            d = json.load(r)
    except Exception:
        return False
    ch = d.get("channels", {})
    hy = bool((ch.get("distance") or {}).get("connected") or (d.get("tof") or {}).get("connected"))
    mk = bool((ch.get("maek_mic") or {}).get("connected"))
    return hy or mk


def child_live(secs):
    """하위 프로세스: ripples.live 시간표 모드 + 기록 쌓기. 보드는 일련번호가 맞을 때만."""
    import ripples.hardware.board as board

    def confirm(port, firmware):
        if port != PORT or not board_ok():
            raise SystemExit("board mismatch")
        print("BOARD AUTO-CONFIRMED", port, SERIAL, firmware, flush=True)
    board.confirm_board = confirm
    sys.argv = ["ripples.live", "--port", PORT, "--accumulate", "--db", str(DB), "--seconds", str(int(secs))]
    runpy.run_module("ripples.live", run_name="__main__")


class Runner:
    """실시간(live) ↔ 저장 데이터(example: play_exhibition_example.py, 녹화 클립 · 예제 기록 · 같은 mp3 · 같은 시계)."""
    def __init__(self, end):
        self.end, self.mode, self.proc, self.ev = end, None, None, None
        self.audio_proc = None
        self.out = open(RUN / "sim-daily.log", "a", encoding="utf-8", buffering=1)

    def stop(self):
        if self.proc is None:
            return
        flag = RUN / ("stop" if self.mode == "live" else "exhibition-example.stop")
        flag.touch()
        if self.ev: self.ev.set()
        if self.audio_proc is not None:
            (RUN / "sim-audio.stop").touch()
            try: self.audio_proc.wait(5)
            except Exception: self.audio_proc.kill(); self.audio_proc.wait(5)
            self.audio_proc = None
        try: self.proc.wait(15)
        except Exception: self.proc.kill(); self.proc.wait(5)
        log("멈춤", self.mode, "코드", self.proc.returncode)
        if self.mode == "live": flag.unlink(missing_ok=True)
        self.proc = self.ev = None

    def start(self, mode):
        import subprocess
        self.stop()
        secs = max(1, int(self.end - time.time()))
        if mode == "live":
            self.proc = subprocess.Popen([PY, "-X", "utf8", "-u", str(Path(__file__).resolve()), "--live", str(secs)],
                                         cwd=ROOT, stdout=self.out, stderr=self.out)
            (RUN / "sim-audio.stop").unlink(missing_ok=True)
            with (RUN / "sim-audio.log").open("a", encoding="utf-8") as audio_log:
                self.audio_proc = subprocess.Popen([PY, "-X", "utf8", "-u", str(Path(__file__).resolve()), "--audio", str(secs)], cwd=ROOT, stdout=audio_log, stderr=audio_log)
        else:
            dm = RUN / "exhibition-display-mode.txt"        # 손으로 고정해 둔 상태가 있으면 시계대로 돌게 치움
            if dm.exists() and dm.read_text(encoding="utf-8", errors="ignore").strip() in ("activate", "sleep"):
                dm.replace(dm.with_suffix(".txt.bak")); log("표시 상태 고정 해제 →", dm.name + ".bak")
            (RUN / "exhibition-example.stop").unlink(missing_ok=True)
            self.proc = subprocess.Popen([PY, "-X", "utf8", "-u", str(ROOT / "scripts" / "play_exhibition_example.py")],
                                         cwd=ROOT, stdout=self.out, stderr=self.out)
        self.mode = mode
        log("시작", "실시간(혈/맥 연결)" if mode == "live" else "저장 데이터(혈 · 맥 연결 전)", f"{secs}초 남음")


def main():
    lock = (RUN / "sim-daily.lock").open("a+b")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return log("이미 실행 중 — 끝")
    if (RUN / "sim-daily.off").exists():
        return log("sim-daily.off 있음(손으로 끔) — 끝")
    now, s, e = window()
    if not (s <= now < e):
        return log("운영 시간 밖", now.strftime("%H:%M"), "— 끝")
    if not board_ok():
        return log("LED 보드", PORT, SERIAL, "없음 — 열지 않고 끝")
    # Initialize PortAudio on the main Windows thread before spawning workers.
    import numpy, sounddevice, soundfile
    log("AUDIO backend initialized on main thread")
    end = time.time() + (e - now).total_seconds()
    (RUN / "exhibition-example.stop").touch()          # 다른 데서 켜 둔 예제 플레이어가 있으면 비킴
    time.sleep(2)
    log("운영 시작 · 19:00 까지 · 기록", DB.name)
    r = Runner(end)
    last_on = last_off = time.time()
    on = connected()
    r.start("live" if on else "example")
    try:
        while time.time() < end and not (RUN / "sim-daily.off").exists():
            time.sleep(5)
            if connected(): last_on = time.time()
            else: last_off = time.time()
            if r.mode == "example" and time.time() - last_off >= LIVE_AFTER_S:
                r.start("live")
            elif r.mode == "live" and time.time() - last_on >= EXAMPLE_AFTER_S:
                r.start("example")
            elif r.proc is not None and r.proc.poll() is not None:
                log("하위 프로세스가 멈춤", r.mode, r.proc.returncode, "— 다시 켬")
                time.sleep(3); r.start(r.mode)
    finally:
        r.stop()
        time.sleep(1.5)
        black()
        log("운영 끝")


if __name__ == "__main__":
    os.chdir(ROOT)
    if len(sys.argv) > 2 and sys.argv[1] == "--live":
        child_live(float(sys.argv[2]))                  # 하위 프로세스(출력은 부모가 로그로 돌림)
    elif len(sys.argv) > 2 and sys.argv[1] == "--audio":
        audio(time.time() + float(sys.argv[2]), threading.Event())
    else:
        sys.stdout = sys.stderr = open(RUN / "sim-daily.log", "a", encoding="utf-8", buffering=1)   # pythonw: 창 없이 로그만
        main()
