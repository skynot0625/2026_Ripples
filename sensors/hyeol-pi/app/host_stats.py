"""Server-host CPU / GPU usage for the simulator screen (2026-10-15).

Only real readings, never invented numbers: a value that cannot be read on this OS / hardware is None (the page shows
"--" / 측정 불가). Collection runs in one background thread every INTERVAL_S seconds and only while someone asked for
it in the last IDLE_STOP_S seconds; requests just read the cached snapshot, so the sensor / render paths never wait.

- CPU % (whole machine, all cores): Windows GetSystemTimes, macOS host_statistics(HOST_CPU_LOAD_INFO), Linux
  /proc/stat — busy ticks / all ticks between two readings. No extra package (psutil is not installed).
- GPU %: NVIDIA via nvidia-smi (utilization.gpu, Windows / Linux), Apple GPU via ioreg IOAccelerator
  PerformanceStatistics "Device Utilization %" (macOS). Other GPUs (e.g. an AMD integrated GPU next to an NVIDIA
  card) are not read; the snapshot names the GPU it measured.
"""
from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
import sys
import threading
import time

INTERVAL_S = 2.0
IDLE_STOP_S = 30.0
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0   # never flash a console window on the exhibition PC


def _cpu_ticks_windows() -> tuple[int, int] | None:
    ft = [ctypes.c_ulonglong() for _ in range(3)]
    if not ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(f) for f in ft)):
        return None
    idle, kernel, user = (f.value for f in ft)   # kernel time includes idle time
    return kernel + user - idle, kernel + user


_mac = None


def _cpu_ticks_mac() -> tuple[int, int] | None:
    global _mac
    if _mac is None:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        lib.mach_host_self.restype = ctypes.c_uint
        _mac = lib
    ticks, count = (ctypes.c_uint * 4)(), ctypes.c_uint(4)   # user, system, idle, nice
    if _mac.host_statistics(_mac.mach_host_self(), 3, ticks, ctypes.byref(count)) != 0:
        return None
    user, system, idle, nice = ticks
    return user + system + nice, user + system + idle + nice


def _cpu_ticks_linux() -> tuple[int, int] | None:
    with open("/proc/stat", encoding="ascii") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v) - idle, sum(v)


def cpu_ticks() -> tuple[int, int] | None:
    try:
        if sys.platform == "win32":
            return _cpu_ticks_windows()
        if sys.platform == "darwin":
            return _cpu_ticks_mac()
        return _cpu_ticks_linux()
    except (OSError, AttributeError, ValueError):
        return None


def _run(cmd: list[str]) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3, creationflags=_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def gpu_reading() -> dict[str, object]:
    """{"gpu": % or None, "gpu_name": str or None, "gpu_note": why None / what was measured}."""
    if sys.platform == "darwin":
        out = _run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"])
        vals = [int(m) for m in re.findall(r'"Device Utilization %"=(\d+)', out or "")]
        if vals:
            return {"gpu": float(max(vals)), "gpu_name": "Apple GPU", "gpu_note": "ioreg Device Utilization"}
        return {"gpu": None, "gpu_name": None, "gpu_note": "ioreg에 GPU 사용률 없음"}
    smi = shutil.which("nvidia-smi")
    if not smi:
        return {"gpu": None, "gpu_name": None, "gpu_note": "nvidia-smi 없음 (이 GPU 사용률은 읽지 않음)"}
    out = _run([smi, "--query-gpu=name,utilization.gpu", "--format=csv,noheader,nounits"])
    rows = []
    for line in (out or "").splitlines():
        name, _, util = line.rpartition(",")
        try:
            rows.append((name.strip(), float(util)))
        except ValueError:
            continue
    if not rows:
        return {"gpu": None, "gpu_name": None, "gpu_note": "nvidia-smi 응답 없음"}
    name, util = max(rows, key=lambda r: r[1])
    return {"gpu": util, "gpu_name": name, "gpu_note": "nvidia-smi (NVIDIA만)"}


def pi_health() -> dict[str, object]:
    """Raspberry Pi only (2026-11-10, Pi 점검 칸): SoC temperature (°C) and the firmware's power / heat flags
    (vcgencmd get_throttled: under-voltage, throttled now / since boot). None where it cannot be read."""
    temp = throttled = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="ascii") as f:
            temp = round(int(f.read().strip()) / 1000, 1)
    except (OSError, ValueError):
        pass
    if shutil.which("vcgencmd"):
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
            throttled = int(out.strip().split("=")[1], 16)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
    return {"temp_c": temp, "throttled": throttled}


class HostStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap: dict[str, object] = {"cpu": None, "gpu": None, "gpu_name": None, "gpu_note": "측정 시작 전", "at": None}
        self._asked = 0.0
        self._thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, object]:
        now = time.monotonic()
        with self._lock:
            self._asked = now
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="host-stats", daemon=True)
                self._thread.start()
            snap = dict(self._snap)
        at = snap.pop("at")
        snap["age_s"] = None if at is None else round(now - at, 1)
        snap["interval_s"] = INTERVAL_S
        return snap

    def _loop(self) -> None:
        prev = cpu_ticks()
        while True:
            time.sleep(INTERVAL_S)
            with self._lock:
                if time.monotonic() - self._asked > IDLE_STOP_S:
                    self._thread = None
                    return
            cur = cpu_ticks()
            cpu = None
            if prev and cur and cur[1] > prev[1]:
                cpu = round(100.0 * (cur[0] - prev[0]) / (cur[1] - prev[1]), 1)
            prev = cur
            snap = dict(gpu_reading(), **pi_health(), cpu=cpu, at=time.monotonic())
            with self._lock:
                self._snap = snap
