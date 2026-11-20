#!/usr/bin/env python3
"""수집단 (collector): ToF 카메라 + 천장 거리 센서를 혼자 읽고, ① 파일로 저장하고 ② 표시단에 실시간으로 보낸다.

    센서 ─▶ collector ─┬─▶ 파일 (DATA_DIR, 형식은 DATA_FORMAT.md)
                        └─▶ run/collector.sock ─▶ 표시단 server.py (--source collector) / 외부 전송 등

카메라 · 직렬 포트는 한 프로그램만 열 수 있어서, 센서를 여는 건 이 프로그램 하나뿐이다. 나머지는 소켓으로 받는다.
설정은 settings.sh 를 그대로 읽는다 (CAMERA_INDEX, DISTANCE_PORT, DISTANCE_BAUD, DATA_DIR, RECORD, TOF_SAVE_FPS,
DATA_BUDGET_GB, MIN_FREE_GB). systemd 서비스 ripples-collector 로 부팅 때 자동으로 켜진다 (setup_pi.sh).

    ~/tof-env/bin/python collector/collector.py            # settings.sh 기준
    ~/tof-env/bin/python collector/collector.py --mock     # 센서 없이 가짜 데이터 (개발용)
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import json
import math
import os
import re
import select
import shutil
import signal
import socket
import sys
import threading
import time
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import collector_protocol as proto  # noqa: E402
import tofsense  # noqa: E402

SOCK_PATH = ROOT / "run" / "collector.sock"
STATUS_PATH = ROOT / "run" / "collector_status.json"
RANGE_MM = 4000
CONF_MIN_SUMMARY = 30          # 요약 통계에서 "유효 픽셀"로 칠 confidence (표시단 기본값과 같음)
FORMAT_VERSION = 1


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- 설정 -------------------------------------------------
DEFAULTS = {"CAMERA_INDEX": "0", "DISTANCE_PORT": "auto", "DISTANCE_BAUD": "0", "DATA_DIR": "~/ripples-data",
            "RECORD": "1", "TOF_SAVE_FPS": "all", "TOF_CHUNK_SECONDS": "10", "TOF_COMPRESS": "1",
            "DATA_BUDGET_GB": "14", "MIN_FREE_GB": "3"}


def load_settings(path: Path) -> dict[str, str]:
    cfg = dict(DEFAULTS)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z_]+)=(.*)$", line)
            if m:
                v = m.group(2).split("#")[0].strip().strip('"').strip("'")
                cfg[m.group(1)] = v
    cfg["DATA_DIR"] = os.path.expanduser(os.path.expandvars(cfg["DATA_DIR"]))
    return cfg


# ---------------------------------------------------------------- 실시간 전달 (소켓) ----------------------------------
class Client:
    """표시단 하나. ToF 는 최신 1장만 (밀리면 옛것 버림), 작은 메시지는 큐(최대 2000)에 쌓아 보낸다.
    느린 수신자 때문에 센서 쪽이 멈추는 일은 없다."""

    def __init__(self, sock: socket.socket, hub: "Hub") -> None:
        self.sock, self.hub = sock, hub
        self.cond = threading.Condition()
        self.small: collections.deque[bytes] = collections.deque(maxlen=2000)
        self.tof: bytes | None = None
        self.alive = True
        threading.Thread(target=self._writer, daemon=True).start()

    def push(self, data: bytes, latest_only: bool) -> None:
        with self.cond:
            if latest_only:
                self.tof = data
            else:
                self.small.append(data)
            self.cond.notify()

    def _writer(self) -> None:
        try:
            while self.alive:
                with self.cond:
                    while self.alive and not self.small and self.tof is None:
                        self.cond.wait(1.0)
                    batch = list(self.small)
                    self.small.clear()
                    tof, self.tof = self.tof, None
                for b in batch:
                    self.sock.sendall(b)
                if tof is not None:
                    self.sock.sendall(tof)
        except OSError:
            pass
        finally:
            self.alive = False
            self.hub.drop(self)
            try:
                self.sock.close()
            except OSError:
                pass


class Hub:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.clients: list[Client] = []
        self.lock = threading.Lock()
        self.hello: bytes | None = None

    def serve(self, stop: threading.Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.path))
        os.chmod(self.path, 0o666)
        srv.listen(8)
        srv.settimeout(0.5)
        log(f"소켓 대기: {self.path}")
        while not stop.is_set():
            try:
                s, _ = srv.accept()
            except socket.timeout:
                continue
            c = Client(s, self)
            with self.lock:
                self.clients.append(c)
                hello = self.hello
            if hello:
                c.push(hello, False)
            log(f"표시단 연결 (지금 {len(self.clients)}개)")
        srv.close()
        try:
            self.path.unlink()
        except OSError:
            pass

    def drop(self, c: Client) -> None:
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)
                log(f"표시단 끊김 (지금 {len(self.clients)}개)")

    def set_hello(self, header: dict) -> None:
        data = proto.pack(header)
        with self.lock:
            self.hello = data
        self.publish(data, False)

    def publish(self, data: bytes, latest_only: bool) -> None:
        with self.lock:
            clients = list(self.clients)
        for c in clients:
            c.push(data, latest_only)

    def count(self) -> int:
        with self.lock:
            return len(self.clients)


# ---------------------------------------------------------------- 저장 -------------------------------------------------
LIDAR_COLS = "seq,t_wall_ns,t_mono_ns,sensor_ms,distance_mm,status,strength,valid,raw_hex"
SUMMARY_COLS = "seq,t_wall_ns,t_mono_ns,valid_ratio,center_mm,median_mm,p05_mm,p95_mm"


class HourlyCsv:
    """시간마다 새 CSV. 닫힌 파일은 gzip 으로 압축 (따로 스레드)."""

    def __init__(self, root: Path, stem: str, cols: str) -> None:
        self.root, self.stem, self.cols = root, stem, cols
        self.key, self.f, self.path = None, None, None

    def write(self, t_wall_ns: int, lines: list[str]) -> None:
        t = dt.datetime.fromtimestamp(t_wall_ns / 1e9)
        key = t.strftime("%Y-%m-%d/%H")
        if key != self.key:
            self.close()
            day = self.root / t.strftime("%Y-%m-%d")
            day.mkdir(parents=True, exist_ok=True)
            self.path = day / f"{self.stem}_{t.strftime('%Y%m%d_%H')}.csv"
            new = not self.path.exists()
            self.f = open(self.path, "a", encoding="utf-8", buffering=1 << 16)
            if new:
                self.f.write(self.cols + "\n")
            self.key = key
        self.f.write("".join(lines))

    def flush(self) -> None:
        if self.f:
            self.f.flush()

    def close(self) -> None:
        if self.f:
            self.f.close()
            threading.Thread(target=gzip_file, args=(self.path,), daemon=True).start()
        self.f, self.key = None, None


def gzip_file(path: Path) -> None:
    try:
        gz = path.with_suffix(path.suffix + ".gz")
        with open(path, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"압축 실패 {path}: {exc}")


class Recorder(threading.Thread):
    """센서 스레드는 큐에 넣기만 하고, 디스크 쓰기는 전부 여기서 (센서가 디스크 때문에 멈추지 않게)."""

    def __init__(self, cfg: dict, stop: threading.Event, meta: dict) -> None:
        super().__init__(daemon=True)
        self.stop = stop
        self.root = Path(cfg["DATA_DIR"])
        self.enabled = cfg["RECORD"] != "0"
        v = str(cfg["TOF_SAVE_FPS"]).strip().lower()
        self.save_all = v in ("all", "-1")       # 모든 프레임 (raw 전체)
        self.save_fps = 0.0 if self.save_all else float(v)
        self.chunk_s = max(1.0, float(cfg.get("TOF_CHUNK_SECONDS", "10")))
        self.compress = int(cfg.get("TOF_COMPRESS", "1"))
        self.tof_pending = 0               # 큐에 쌓인 ToF 프레임 수 (메모리 보호)
        self.budget = float(cfg["DATA_BUDGET_GB"]) * 1e9
        self.min_free = float(cfg["MIN_FREE_GB"]) * 1e9
        self.q: collections.deque = collections.deque(maxlen=20000)
        self.cond = threading.Condition()
        self.lidar = HourlyCsv(self.root, "lidar", LIDAR_COLS)
        self.summary = HourlyCsv(self.root, "tof_summary", SUMMARY_COLS)
        self.chunk: list[tuple] = []
        self.chunk_key = None
        self.last_saved_frame = 0.0
        self.frames_paused = False      # 디스크 부족으로 프레임 저장을 멈춘 상태
        self.used = 0.0
        self.free = 0.0
        self.meta = meta
        self.dropped = 0

    # 센서 스레드에서 부름
    def put(self, item: tuple) -> None:
        if not self.enabled:
            return
        with self.cond:
            if item[0] == "tof":
                if self.tof_pending >= 300:   # 디스크가 못 따라오면 (약 10초치 넘게 밀리면) ToF 프레임은 버림
                    self.dropped += 1
                    return
                self.tof_pending += 1
            if len(self.q) == self.q.maxlen:
                self.dropped += 1
            self.q.append(item)
            self.cond.notify()

    def run(self) -> None:
        if not self.enabled:
            log("저장 꺼짐 (RECORD=0) · 실시간 전달만 함")
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._recover()
        self._write_session()
        last_flush = last_janitor = 0.0
        while True:
            with self.cond:
                if not self.q:
                    self.cond.wait(0.5)
                items = list(self.q)
                self.q.clear()
                self.tof_pending = 0
            lidar_lines: dict[int, list[str]] = {}
            for it in items:
                if it[0] == "lidar":
                    _, h = it
                    lidar_lines.setdefault(h["t_wall_ns"], []).append(
                        f"{h['seq']},{h['t_wall_ns']},{h['t_mono_ns']},{h['sensor_ms']},{h['distance_mm']},"
                        f"{h['status']},{h['strength']},{int(h['valid'])},{h['raw_hex']}\n")
                else:
                    self._tof(*it[1:])
            for t, lines in lidar_lines.items():
                self.lidar.write(t, lines)
            now = time.monotonic()
            if now - last_flush >= 1.0:
                self.lidar.flush()
                self.summary.flush()
                last_flush = now
            if now - last_janitor >= 30.0:
                self._janitor()
                last_janitor = now
            if self.stop.is_set() and not self.q:
                break
        self._write_chunk()
        self.lidar.close()
        self.summary.close()
        time.sleep(0.5)   # gzip 스레드가 끝날 시간
        log("저장 종료")

    def _tof(self, seq: int, t_wall: int, t_mono: int, depth: np.ndarray, conf: np.ndarray) -> None:
        valid = (depth > 0) & (conf >= CONF_MIN_SUMMARY)
        vals = depth[valid]
        h, w = depth.shape
        c = depth[h // 2 - 2:h // 2 + 3, w // 2 - 2:w // 2 + 3]
        c = c[c > 0]
        if vals.size:
            p05, med, p95 = np.percentile(vals, (5, 50, 95))
            stats = f"{vals.size / depth.size:.4f},{int(np.median(c)) if c.size else ''},{int(med)},{int(p05)},{int(p95)}"
        else:
            stats = f"0,{int(np.median(c)) if c.size else ''},,,"
        self.summary.write(t_wall, [f"{seq},{t_wall},{t_mono},{stats}\n"])
        if self.frames_paused or (not self.save_all and self.save_fps <= 0):
            return
        if not self.save_all:
            if t_mono / 1e9 - self.last_saved_frame < 1.0 / self.save_fps - 1e-3:
                return
            self.last_saved_frame = t_mono / 1e9
        sec = t_wall / 1e9
        sec -= sec % self.chunk_s
        key = dt.datetime.fromtimestamp(sec).strftime("%Y-%m-%d/%H%M%S")
        if key != self.chunk_key:
            self._write_chunk()
            self.chunk_key = key
        self.chunk.append((seq, t_wall, t_mono, depth, np.minimum(conf, 255).astype(np.uint8)))

    def _write_chunk(self) -> None:
        if not self.chunk:
            return
        day, hm = self.chunk_key.split("/")
        d = self.root / day / "tof_frames"
        d.mkdir(parents=True, exist_ok=True)
        stamp = day.replace("-", "") + "_" + hm   # HHMMSS (TOF_CHUNK_SECONDS 단위)
        path = d / f"tof_{stamp}.npz"
        n = 1
        while path.exists():
            path = d / f"tof_{stamp}_{n}.npz"
            n += 1
        tmp = path.with_suffix(".tmp.npz")
        rows = self.chunk
        arrays = {"seq": np.array([r[0] for r in rows], np.int64),
                  "t_wall_ns": np.array([r[1] for r in rows], np.int64),
                  "t_mono_ns": np.array([r[2] for r in rows], np.int64),
                  "depth": np.stack([r[3] for r in rows]),
                  "conf": np.stack([r[4] for r in rows])}
        # np.load 로 그대로 읽히는 .npz (zip). 압축 수준은 낮게 (Pi CPU 로 30 fps 를 따라가도록)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=self.compress) as zf:
            for name, arr in arrays.items():
                with zf.open(name + ".npy", "w", force_zip64=True) as f:
                    np.lib.format.write_array(f, arr, allow_pickle=False)
        os.replace(tmp, path)
        self.chunk = []

    def _write_session(self) -> None:
        start = dt.datetime.now()
        day = self.root / start.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        doc = dict(self.meta, format_version=FORMAT_VERSION, started=start.isoformat(timespec="seconds"),
                   tof_save_fps="all" if self.save_all else self.save_fps, chunk_seconds=self.chunk_s, budget_gb=self.budget / 1e9, min_free_gb=self.min_free / 1e9)
        (day / f"session_{start.strftime('%H%M%S')}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1))

    def update_meta(self, meta: dict) -> None:
        self.meta.update(meta)
        if self.enabled:
            day = self.root / dt.datetime.now().strftime("%Y-%m-%d")
            day.mkdir(parents=True, exist_ok=True)
            (day / "tof_camera.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))

    def _recover(self) -> None:
        """지난번에 갑자기 꺼져서 남은 .csv / .tmp.npz 정리."""
        for p in self.root.rglob("*.csv"):
            gzip_file(p)
        for p in self.root.rglob("*.tmp.npz"):
            p.unlink(missing_ok=True)

    def _janitor(self) -> None:
        """용량 관리: DATA_DIR 가 DATA_BUDGET_GB 를 넘거나 디스크 남은 공간이 MIN_FREE_GB 아래면 오래된 파일부터 지운다.
        지울 게 없는데도 공간이 부족하면 ToF 프레임 저장만 멈춘다 (거리 · 요약 CSV 는 작아서 계속)."""
        files = [p for p in self.root.rglob("*") if p.is_file()]
        sizes = {p: p.stat().st_size for p in files}
        self.used = float(sum(sizes.values()))
        self.free = float(shutil.disk_usage(self.root).free)
        open_now = {self.lidar.path, self.summary.path}
        # 지우는 순서: 오래된 ToF 프레임 → 오래된 CSV (지금 쓰는 CSV · 세션 정보는 남김)
        old = sorted((p for p in files if p not in open_now and not p.name.endswith(".json") and not p.name.endswith(".tmp.npz")),
                     key=lambda p: (0 if p.suffix == ".npz" else 1, p.stat().st_mtime))
        deleted = 0
        for p in old:
            if self.used <= self.budget and self.free >= self.min_free:
                break
            try:
                p.unlink()
                self.used -= sizes[p]
                self.free += sizes[p]
                deleted += 1
            except OSError:
                pass
        if deleted:
            log(f"용량 관리: 오래된 파일 {deleted}개 지움 · 지금 {self.used / 1e9:.2f} GB")
        for d in sorted(self.root.glob("*/tof_frames")):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        paused = self.free < self.min_free
        if paused != self.frames_paused:
            log("디스크 부족: ToF 프레임 저장 멈춤" if paused else "디스크 여유 회복: ToF 프레임 저장 다시 시작")
        self.frames_paused = paused

    def status(self) -> dict:
        return {"enabled": self.enabled, "data_dir": str(self.root), "used_gb": round(self.used / 1e9, 3),
                "free_gb": round(self.free / 1e9, 2), "budget_gb": self.budget / 1e9, "min_free_gb": self.min_free / 1e9,
                "tof_save_fps": "all" if self.save_all else self.save_fps,
                "saving_frames": self.enabled and (self.save_all or self.save_fps > 0) and not self.frames_paused,
                "queue": len(self.q), "dropped": self.dropped}


# ---------------------------------------------------------------- 센서 -------------------------------------------------
class Rate:
    def __init__(self) -> None:
        self.t: collections.deque[float] = collections.deque(maxlen=200)

    def tick(self, now: float) -> None:
        self.t.append(now)

    def hz(self, now: float) -> float:
        recent = [x for x in self.t if now - x <= 2.0]
        return round((len(recent) - 1) / (recent[-1] - recent[0]), 1) if len(recent) > 1 and recent[-1] > recent[0] else 0.0


class ToFSource(threading.Thread):
    def __init__(self, hub: Hub, rec: Recorder, stop: threading.Event, index: int, mock: bool) -> None:
        super().__init__(daemon=True)
        self.hub, self.rec, self.stop, self.index, self.mock = hub, rec, stop, index, mock
        self.seq = 0
        self.rate = Rate()
        self.last = 0.0
        self.error: str | None = "아직 안 열림"
        self.resolution = None

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self._run_camera()
            except Exception as exc:
                self.error = str(exc)
                log(f"ToF 카메라 오류: {exc} · 3초 뒤 다시")
                self.stop.wait(3.0)

    def _run_camera(self) -> None:
        if self.mock:
            cam, info, intr = None, (240, 180), None
        else:
            import ArducamDepthCamera as ac
            cam = ac.ArducamCamera()
            if cam.open(ac.Connection.CSI, self.index) != 0:
                raise RuntimeError(f"카메라 열기 실패 (index {self.index}) · 리본 케이블 · 카메라 전원")
            if cam.start(ac.FrameType.DEPTH) != 0:
                cam.close()
                raise RuntimeError("카메라 시작 실패")
            try:
                cam.setControl(ac.Control.RANGE, RANGE_MM)
            except Exception:
                pass
            ci = cam.getCameraInfo()
            info = (ci.width, ci.height)
            try:
                intr = {k: cam.getControl(getattr(ac.Control, f"INTRINSIC_{k.upper()}")) / 100 for k in ("fx", "fy", "cx", "cy")}
            except Exception:
                intr = None
        self.resolution = f"{info[0]}x{info[1]}"
        meta = {"width": info[0], "height": info[1], "intrinsics": intr, "range_mm": RANGE_MM,
                "camera": "mock" if self.mock else "Arducam ToF (ArducamDepthCamera SDK)"}
        self.hub.set_hello({"type": "hello", "version": proto.VERSION, "tof": meta,
                            "lidar": {"model": "Nooploop TOFSense (NLink frame0, UART)"}})
        self.rec.update_meta({"tof": meta})
        log(f"ToF 카메라 열림: {self.resolution} · intrinsics {intr}")
        self.error = None
        try:
            t0 = time.monotonic()
            while not self.stop.is_set():
                if self.mock:
                    time.sleep(1 / 30)
                    t = time.monotonic() - t0
                    yy, xx = np.mgrid[0:info[1], 0:info[0]]
                    depth = (1500 + 800 * np.sin(xx / 30 + t) + 300 * np.cos(yy / 20)).astype(np.uint16)
                    conf = np.full_like(depth, 120)
                else:
                    f = cam.requestFrame(2000)
                    if f is None:
                        continue
                    try:
                        depth = np.clip(np.rint(np.asarray(f.depth_data, dtype=np.float32)), 0, 65535).astype(np.uint16)
                        conf = np.clip(np.rint(np.asarray(f.confidence_data, dtype=np.float32)), 0, 65535).astype(np.uint16)
                    finally:
                        cam.releaseFrame(f)
                t_wall, t_mono = time.time_ns(), time.monotonic_ns()
                self.seq += 1
                h, w = depth.shape
                header = {"type": "tof", "seq": self.seq, "t_wall_ns": t_wall, "t_mono_ns": t_mono,
                          "width": w, "height": h, "depth_dtype": "<u2", "conf_dtype": "<u2"}
                d = depth.astype("<u2", copy=False)
                c = conf.astype("<u2", copy=False)
                self.hub.publish(proto.pack(header, d.tobytes() + c.tobytes()), True)
                self.rec.put(("tof", self.seq, t_wall, t_mono, depth, conf))
                now = time.monotonic()
                self.rate.tick(now)
                self.last = now
        finally:
            if cam is not None:
                try:
                    cam.stop()
                finally:
                    cam.close()

    def status(self, now: float) -> dict:
        fresh = now - self.last < 3.0
        return {"connected": fresh and self.error is None, "fps": self.rate.hz(now) if fresh else 0.0,
                "frames": self.seq, "resolution": self.resolution, "error": self.error,
                "age_s": round(now - self.last, 2) if self.last else None}


class RawParser:
    """tofsense.FrameParser 와 같은 재동기화 규칙, 대신 16바이트 전체 필드를 돌려줌."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad = 0

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(bytes(tofsense.HEADER))
            if i < 0:
                del self.buf[:max(0, len(self.buf) - 1)]
                return out
            del self.buf[:i]
            if len(self.buf) < tofsense.FRAME_LEN:
                return out
            fr = bytes(self.buf[:tofsense.FRAME_LEN])
            if tofsense.checksum_ok(fr):
                out.append(fr)
                del self.buf[:tofsense.FRAME_LEN]
            else:
                self.bad += 1
                del self.buf[:1]


class LidarSource(threading.Thread):
    def __init__(self, hub: Hub, rec: Recorder, stop: threading.Event, port: str, baud: int | None, mock: bool) -> None:
        super().__init__(daemon=True)
        self.hub, self.rec, self.stop, self.port, self.baud, self.mock = hub, rec, stop, port, baud, mock
        self.seq = 0
        self.rate = Rate()
        self.last = 0.0
        self.last_mm = None
        self.where = None
        self.error: str | None = "아직 못 찾음"
        self.bad = 0

    def _emit(self, fr: bytes) -> None:
        mm = int.from_bytes(fr[8:11], "little", signed=True)
        status, strength = fr[11], fr[12] | fr[13] << 8
        self.seq += 1
        h = {"type": "lidar", "seq": self.seq, "t_wall_ns": time.time_ns(), "t_mono_ns": time.monotonic_ns(),
             "sensor_ms": int.from_bytes(fr[4:8], "little"), "distance_mm": mm, "status": status,
             "strength": strength, "id": fr[3], "valid": proto.lidar_valid(mm, status), "raw_hex": fr.hex()}
        self.hub.publish(proto.pack(h), False)
        self.rec.put(("lidar", h))
        now = time.monotonic()
        self.rate.tick(now)
        self.last = now
        self.last_mm = mm if h["valid"] else None

    def run(self) -> None:
        if self.mock:
            self.where, self.error = "mock", None
            t0 = time.monotonic()
            while not self.stop.is_set():
                time.sleep(0.02)
                t = time.monotonic() - t0
                self._emit(tofsense.frame(int(2200 + 150 * math.sin(t * 0.7)), status=1, t_ms=int(t * 1000)))
            return
        last_msg = None
        while not self.stop.is_set():
            found = tofsense.find(self.port, self.baud)
            if found is None:
                ports = tofsense.candidate_ports(self.port)
                self.error = (f"신호 없음 (찾아본 포트: {', '.join(ports)}) · TX↔RX · 5V · GND" if ports
                              else "포트 없음 · GPIO UART 설정 (setup_pi.sh) 확인")
                if self.error != last_msg:
                    log(f"천장 거리 센서: {self.error}")
                    last_msg = self.error
                self.stop.wait(2.0)
                continue
            path, baud, fd, _ = found
            self.where, self.error, last_msg = f"{path} · {baud}", None, None
            log(f"천장 거리 센서: {path} · {baud} baud 에서 받는 중")
            parser, quiet = RawParser(), time.monotonic()
            try:
                while not self.stop.is_set():
                    r, _, _ = select.select([fd], [], [], 0.25)
                    if r:
                        data = os.read(fd, 1024)
                        if not data:
                            break
                        frames = parser.feed(data)
                        for fr in frames:
                            self._emit(fr)
                        if frames:
                            quiet = time.monotonic()
                        self.bad = parser.bad
                    if time.monotonic() - quiet > 2.0:
                        self.error = "신호 끊김 (2초 넘게 값 없음)"
                        log(f"천장 거리 센서: {self.error} · 다시 찾는 중")
                        break
            except OSError as exc:
                self.error = f"포트 오류: {exc}"
            finally:
                os.close(fd)
                self.where = None

    def status(self, now: float) -> dict:
        fresh = now - self.last < 3.0
        return {"connected": fresh and self.error is None, "hz": self.rate.hz(now) if fresh else 0.0,
                "frames": self.seq, "distance_m": round(self.last_mm / 1000, 3) if fresh and self.last_mm else None,
                "port": self.where, "error": self.error, "bad_checksum": self.bad,
                "age_s": round(now - self.last, 2) if self.last else None}


# ---------------------------------------------------------------- main -------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", default=str(ROOT / "settings.sh"))
    ap.add_argument("--mock", action="store_true", help="센서 없이 가짜 ToF · 거리 데이터")
    ap.add_argument("--no-record", action="store_true", help="저장하지 않고 실시간 전달만")
    a = ap.parse_args()
    cfg = load_settings(Path(a.settings))
    if a.no_record:
        cfg["RECORD"] = "0"
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    hub = Hub(SOCK_PATH)
    rec = Recorder(cfg, stop, {"settings": {k: cfg[k] for k in DEFAULTS}, "host": socket.gethostname()})
    tof = ToFSource(hub, rec, stop, int(cfg["CAMERA_INDEX"]), a.mock)
    baud = int(cfg["DISTANCE_BAUD"]) or None
    lidar = LidarSource(hub, rec, stop, cfg["DISTANCE_PORT"], baud, a.mock)
    threading.Thread(target=hub.serve, args=(stop,), daemon=True).start()
    rec.start()
    tof.start()
    lidar.start()
    log(f"collector 시작 · 저장 {'켬 → ' + cfg['DATA_DIR'] if cfg['RECORD'] != '0' else '끔'}"
        f" · ToF 프레임 저장 {cfg['TOF_SAVE_FPS']} fps · 한도 {cfg['DATA_BUDGET_GB']} GB")
    last_line = 0.0
    while not stop.wait(1.0):
        now = time.monotonic()
        st = {"type": "status", "t_wall_ns": time.time_ns(), "tof": tof.status(now), "lidar": lidar.status(now),
              "disk": rec.status(), "clients": hub.count()}
        hub.publish(proto.pack(st), False)
        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False))
        os.replace(tmp, STATUS_PATH)
        if now - last_line >= 60:
            t, l, d = st["tof"], st["lidar"], st["disk"]
            log(f"상태 · ToF {t['fps']} fps ({t['error'] or 'ok'}) · 거리 {l['hz']} Hz {l['distance_m']} m "
                f"({l['error'] or 'ok'}) · 저장 {d['used_gb']} GB / 남은 디스크 {d['free_gb']} GB")
            last_line = now
    log("끄는 중…")
    rec.join(timeout=10)


if __name__ == "__main__":
    main()
