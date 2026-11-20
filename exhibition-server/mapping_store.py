"""Server-authoritative mapping for the exhibition booths (master canvas + blind surfaces).

The server keeps the latest mapping in memory and persists it to a JSON file next to server.py
(default data/mapping.json). Every browser (phone /mapping?test, the space outputs /output/<role>?space=a|b&clean) reads and
writes through the server, so all of them show the same values.

File schema (version 2):
    {"version": 2, "revision": 12, "updated_at": "2026-10-13T17:05:00+09:00",
     "booths": {
       "a": {"name": "부스 A", "aspect": 2.0, "gap_mode": "include", "content": "pattern", "source_zoom": 1.0,
             "surfaces": [{"id": "1", "name": "블라인드 1",
                           "rect": {"x": 0.0, "y": 0.0, "w": 0.45, "h": 1.0},
                           "corners": [[x, y], [x, y], [x, y], [x, y]]}, ...]},
       "b": {...}},
     "assignment": {"a": "hyeol", "b": "maek"}}
- Booth = space. Space "a" is the projector window opened at /output/hyeol?space=a&clean, space "b" at /output/maek?space=b&clean (each follows the assignment); its blinds, corners,
  gap mode and output window never move when the roles rotate.
- assignment: which role screen each space shows (혈 "hyeol" / 맥 "maek"). The daily rotation is the "swap" operation,
  which changes only this field. Files without it (written before the rotation existed) load as A=혈, B=맥.
- aspect: master canvas width / height (placeholder until the installation is measured).
- rect: the part of the master canvas this blind shows, 0–1 of the canvas, including the real gaps between blinds.
- gap_mode: "include" crops rect as stored; "ignore" removes the empty space between blinds when cropping
  (computed in mapout.js). Both use the same stored rects, so switching loses nothing.
- content: "pattern" (connection test pattern) or "source" (the output page's own diagnostic visualization).
- source_zoom: uniform scale of the source artwork before it is split across the blinds, 1–2. Width and height use the
  same scale, so the artwork ratio and mapping corners do not change. Files without it load as 1.
- corners: 4-corner warp in the output viewport, 0–1, TL, TR, BR, BL (same meaning as version 1).
- real_w / real_h (optional, 2026-10-15 night): the blind's measured width and height, any unit as long as both use
  the same one (only their ratio is used). Kept only when given and > 0; a blind without both is drawn exactly as
  before. With both, the part of the master canvas the blind shows (its crop) is narrowed, centred, to that real
  width / height ratio — the undistorted content plane — and the corners then warp that plane in perspective as
  before. The measured sizes never move the corners or change rect, and are stored apart from them.

Version 1 files ({"surfaces": {"a": corners, "b": corners}}) are migrated on load: they become booth A's two blinds
and the original file is kept as mapping.v1-backup.json next to it.

Safety: a write goes to <file>.tmp first, is fsynced, the previous good file is copied to <file>.bak, then
the temp file replaces the target with os.replace (atomic on macOS and Windows). Invalid requests are rejected
before anything changes. A damaged file at startup is copied aside (<file>.corrupt-<time>) and the .bak is used.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

BOOTHS = ("a", "b")
# Role screens, separate from the spaces. Until the AI visualisation exists: 혈 = ToF diagnostic page (index.html),
# 맥 = audio diagnostic page (output_b.html). Which role each space's output shows is the "assignment".
ROLES = ("hyeol", "maek")
ROLE_NAMES = {"hyeol": "혈", "maek": "맥"}
DEFAULT_ASSIGNMENT = {"a": "hyeol", "b": "maek"}
MAX_SURFACES = 16
GAP_MODES = ("include", "ignore")
CONTENTS = ("pattern", "source")
DEBOUNCE_SECONDS = 0.5
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# Version 1 defaults (the two test surfaces); reused as booth defaults so nothing moves for existing users.
V1_CORNERS = {
    "a": [[0.05, 0.15], [0.45, 0.15], [0.45, 0.85], [0.05, 0.85]],
    "b": [[0.55, 0.15], [0.95, 0.15], [0.95, 0.85], [0.55, 0.85]],
}


def corners_for_rect(rect: dict) -> list[list[float]]:
    """Default warp for a new blind: its master-canvas rect placed in the output viewport with a 5% margin."""
    x0, y0 = 0.05 + rect["x"] * 0.9, 0.15 + rect["y"] * 0.7
    x1, y1 = 0.05 + (rect["x"] + rect["w"]) * 0.9, 0.15 + (rect["y"] + rect["h"]) * 0.7
    return [[round(x0, 4), round(y0, 4)], [round(x1, 4), round(y0, 4)], [round(x1, 4), round(y1, 4)], [round(x0, 4), round(y1, 4)]]


def default_booth(booth: str) -> dict:
    return {
        "name": f"부스 {booth.upper()}", "aspect": 2.0, "gap_mode": "include", "content": "pattern", "source_zoom": 1.0,
        "surfaces": [
            {"id": "1", "name": "블라인드 1", "rect": {"x": 0.0, "y": 0.0, "w": 0.45, "h": 1.0}, "corners": copy.deepcopy(V1_CORNERS["a"])},
            {"id": "2", "name": "블라인드 2", "rect": {"x": 0.55, "y": 0.0, "w": 0.45, "h": 1.0}, "corners": copy.deepcopy(V1_CORNERS["b"])},
        ],
    }


def default_state() -> dict:
    return {b: default_booth(b) for b in BOOTHS}


# --- validation ------------------------------------------------------------------------------------------------
def _num(v: object, lo: float, hi: float, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"{what} must be a number in {lo}–{hi}")
    return float(v)


def v_corners(value: object) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("corners: 4 corners required")
    out = []
    for p in value:
        if not isinstance(p, list) or len(p) != 2:
            raise ValueError("corners: each corner must be [x, y]")
        out.append([_num(p[0], 0, 1, "corner"), _num(p[1], 0, 1, "corner")])
    return out


def v_rect(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("rect must be an object")
    r = {k: _num(value.get(k), 0, 1, f"rect.{k}") for k in ("x", "y", "w", "h")}
    if r["w"] < 0.01 or r["h"] < 0.01:
        raise ValueError("rect width/height must be at least 0.01")
    if r["x"] + r["w"] > 1 + 1e-6 or r["y"] + r["h"] > 1 + 1e-6:
        raise ValueError("rect must stay inside the master canvas")
    return r


def v_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 40 or any(ord(c) < 32 for c in value):
        raise ValueError("name must be 1–40 characters")
    return value.strip()


def v_real(value: object, what: str) -> float:
    """A measured size: a number > 0 (0, negative or not a number is not a measurement)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1e6:
        raise ValueError(f"{what} must be a number > 0 (up to 1000000)")
    return float(value)


REAL_KEYS = ("real_w", "real_h")


def v_surface(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("surface must be an object")
    sid = value.get("id")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise ValueError("surface id must be 1–32 letters, digits, - or _")
    out = {"id": sid, "name": v_name(value.get("name")), "rect": v_rect(value.get("rect")), "corners": v_corners(value.get("corners"))}
    out.update({k: v_real(value[k], k) for k in REAL_KEYS if value.get(k) is not None})   # only when measured
    return out


def _choice(options: tuple[str, ...], what: str):
    def check(v: object) -> str:
        if v not in options:
            raise ValueError(f"{what} must be one of {list(options)}")
        return v
    return check


def v_booth_fields(value: dict, partial: bool) -> dict:
    out = {}
    checks = {"name": v_name, "aspect": lambda v: _num(v, 0.1, 20, "aspect"),
              "gap_mode": _choice(GAP_MODES, "gap_mode"), "content": _choice(CONTENTS, "content"),
              "source_zoom": lambda v: _num(v, 1, 2, "source_zoom")}
    for key, check in checks.items():
        if key in value:
            out[key] = check(value[key])
        elif not partial and key == "source_zoom":
            out[key] = 1.0
        elif not partial:
            raise ValueError(f"booth.{key} missing")
    return out


def v_booth(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("booth must be an object")
    booth = v_booth_fields(value, partial=False)
    surfaces = value.get("surfaces")
    if not isinstance(surfaces, list) or not 1 <= len(surfaces) <= MAX_SURFACES:
        raise ValueError(f"booth needs 1–{MAX_SURFACES} surfaces")
    booth["surfaces"] = [v_surface(s) for s in surfaces]
    if len({s["id"] for s in booth["surfaces"]}) != len(booth["surfaces"]):
        raise ValueError("surface ids must be unique")
    return booth


def v_state(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != set(BOOTHS):
        raise ValueError(f"booths must contain {list(BOOTHS)}")
    return {b: v_booth(value[b]) for b in BOOTHS}


def v_assignment(value: object) -> dict:
    """Each space shows exactly one role screen and the two spaces show different roles."""
    if not isinstance(value, dict) or set(value) != set(BOOTHS) or sorted(value.values()) != sorted(ROLES):
        raise ValueError(f"assignment must give each of {list(BOOTHS)} one of {list(ROLES)}, all different")
    return {b: value[b] for b in BOOTHS}


def migrate_v1(data: dict) -> dict:
    """Version 1 (two test surfaces in one output) -> booth A's two blinds; booth B gets defaults."""
    surfaces = data.get("surfaces")
    if not isinstance(surfaces, dict) or set(surfaces) != {"a", "b"}:
        raise ValueError("not a version 1 mapping")
    state = default_state()
    state["a"]["surfaces"][0]["corners"] = v_corners(surfaces["a"])
    state["a"]["surfaces"][1]["corners"] = v_corners(surfaces["b"])
    return state


class MappingStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.condition = threading.Condition()
        self.write_lock = threading.Lock()
        self.booths = default_state()
        self.assignment = dict(DEFAULT_ASSIGNMENT)
        self.source = "default"  # default | file | backup | migrated-v1
        self.revision = 0
        self.saved_revision = 0
        self.version = 0  # bumps on any change clients should hear about (values or save state)
        self.origin: str | None = None
        self.seq: int | None = None
        self.final = True
        self.save_error: str | None = None
        self._dirty_at: float | None = None
        self._stop = threading.Event()
        self._load()
        threading.Thread(target=self._writer, daemon=True).start()

    # --- load / save -------------------------------------------------------------------------------------------
    def _read(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level must be an object")
            revision = max(0, int(data.get("revision", 0)))
            if data.get("version", 1) == 1 and "surfaces" in data and "booths" not in data:
                backup = path.with_name("mapping.v1-backup.json")
                if not backup.exists():
                    shutil.copy2(path, backup)
                print(f"Mapping version 1 migrated (original kept as {backup.name})", flush=True)
                return {"booths": migrate_v1(data), "assignment": dict(DEFAULT_ASSIGNMENT), "revision": revision, "migrated": True}
            # files written before the rotation existed have no "assignment": start from the default (A=혈, B=맥)
            assignment = v_assignment(data["assignment"]) if "assignment" in data else dict(DEFAULT_ASSIGNMENT)
            return {"booths": v_state(data.get("booths")), "assignment": assignment, "revision": revision, "migrated": False}
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            print(f"Mapping file ignored ({path.name}): {exc}", flush=True)
            if path == self.path:
                aside = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
                try:
                    shutil.copy2(path, aside)
                    print(f"Damaged mapping file kept as {aside.name}", flush=True)
                except OSError:
                    pass
            return None

    def _load(self) -> None:
        for path, source in ((self.path, "file"), (self.path.with_name(self.path.name + ".bak"), "backup")):
            data = self._read(path)
            if data:
                self.booths, self.assignment, self.revision = data["booths"], data["assignment"], data["revision"]
                # A migrated file keeps its version 1 content on disk until the first edit is saved as version 2.
                self.source = "migrated-v1" if data["migrated"] else source
                self.saved_revision = self.revision
                print(f"Mapping loaded from {path.name} (revision {self.revision})", flush=True)
                return
        print("Mapping: no saved file, using defaults", flush=True)

    def _write(self) -> None:
        with self.write_lock:
            with self.condition:
                revision = self.revision
                # isoformat instead of strftime("%z"): on Windows %z can give a time zone name instead of an offset
                data = {"version": 2, "revision": revision, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "booths": copy.deepcopy(self.booths), "assignment": dict(self.assignment)}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                if self.path.exists():
                    shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
                for attempt in range(5):  # Windows can briefly lock the target (antivirus, indexer)
                    try:
                        os.replace(tmp, self.path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.05)
                error = None
            except OSError as exc:
                error = str(exc)
                print(f"Mapping save failed: {exc}", flush=True)
            with self.condition:
                if error is None:
                    self.saved_revision = max(self.saved_revision, revision)
                    self.source = "file"
                self.save_error = error
                self.version += 1
                self.condition.notify_all()

    def _writer(self) -> None:
        # Debounced disk writes for in-progress drags; final updates are written immediately by apply().
        while not self._stop.is_set():
            with self.condition:
                self.condition.wait_for(lambda: self._dirty_at is not None or self._stop.is_set(), timeout=1.0)
                due = self._dirty_at
            if due is None:
                continue
            delay = due + DEBOUNCE_SECONDS - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
                continue
            with self.condition:
                self._dirty_at = None
            self._write()

    def stop(self) -> None:
        """Flush pending changes (server shutdown)."""
        with self.condition:
            pending = self.revision != self.saved_revision
            self._dirty_at = None
        if pending:
            self._write()
        self._stop.set()
        with self.condition:
            self.condition.notify_all()

    # --- API ---------------------------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self.condition:
            return {"type": "state", "version": self.version, "revision": self.revision, "booths": copy.deepcopy(self.booths),
                    "assignment": dict(self.assignment), "roles": dict(ROLE_NAMES),
                    "source": self.source, "origin": self.origin, "seq": self.seq, "final": self.final,
                    "saved": self.saved_revision == self.revision, "save_error": self.save_error,
                    "path": str(self.path.name), "max_surfaces": MAX_SURFACES}

    def _apply(self, booths: dict, op: dict) -> None:
        """Mutates booths (a working copy) or raises ValueError."""
        kind, bid = op.get("op"), op.get("booth")
        if bid not in BOOTHS:
            raise ValueError(f"booth must be one of {list(BOOTHS)}")
        booth = booths[bid]
        if kind == "booth":
            fields = op.get("set")
            if not isinstance(fields, dict) or not fields or set(fields) - {"name", "aspect", "gap_mode", "content", "source_zoom"}:
                raise ValueError("booth.set: name, aspect, gap_mode, content or source_zoom")
            booth.update(v_booth_fields(fields, partial=True))
            return
        if kind == "add":
            if len(booth["surfaces"]) >= MAX_SURFACES:
                raise ValueError(f"at most {MAX_SURFACES} surfaces per booth")
            used = {s["id"] for s in booth["surfaces"]}
            n = 1
            while str(n) in used:
                n += 1
            right = max(s["rect"]["x"] + s["rect"]["w"] for s in booth["surfaces"])
            w = 0.2
            rect = {"x": round(min(1 - w, right + 0.05) if right + 0.05 + w <= 1 else 1 - w, 4), "y": 0.0, "w": w, "h": 1.0}
            name = op.get("name")
            booth["surfaces"].append({"id": str(n), "name": v_name(name) if name is not None else f"블라인드 {n}",
                                      "rect": rect, "corners": corners_for_rect(rect)})
            return
        sid = op.get("id")
        index = next((i for i, s in enumerate(booth["surfaces"]) if s["id"] == sid), None)
        if index is None:
            raise ValueError(f"no surface {sid!r} in booth {bid}")
        if kind == "delete":
            if len(booth["surfaces"]) <= 1:
                raise ValueError("a booth keeps at least one surface")
            booth["surfaces"].pop(index)
            return
        if kind == "surface":
            fields = op.get("set")
            if not isinstance(fields, dict) or not fields or set(fields) - {"name", "rect", "corners", *REAL_KEYS}:
                raise ValueError("surface.set: name, rect, corners, real_w or real_h")
            checks = {"name": v_name, "rect": v_rect, "corners": v_corners, "real_w": lambda v: v_real(v, "real_w"), "real_h": lambda v: v_real(v, "real_h")}
            surface = booth["surfaces"][index]
            for k, v in fields.items():
                if k in REAL_KEYS and v is None:
                    surface.pop(k, None)   # measured size cleared (empty field)
                else:
                    surface[k] = checks[k](v)
            return
        raise ValueError("op must be swap, booth, add, delete or surface")

    def apply(self, op: object, final: bool, origin: str | None = None, seq: int | None = None) -> dict:
        """Apply one operation. Raises ValueError without changing anything if the request is invalid."""
        if not isinstance(op, dict):
            raise ValueError("operation must be an object")
        swap = op.get("op") == "swap"  # daily rotation: exchange the role screens, booths stay exactly as they are
        if not swap:
            with self.condition:
                work = copy.deepcopy(self.booths)
            self._apply(work, op)
        with self.condition:
            if swap:
                a, b = BOOTHS
                self.assignment = {a: self.assignment[b], b: self.assignment[a]}
            else:
                # Re-apply on the current state so a concurrent change to another surface is not lost.
                current = copy.deepcopy(self.booths)
                self._apply(current, op)
                self.booths = current
            self.revision += 1
            self.version += 1
            self.origin, self.seq, self.final = origin, seq, final
            self._dirty_at = None if final else (self._dirty_at or time.monotonic())
            self.condition.notify_all()
        if final:
            self._write()  # the last position of a drag is always on disk before clients hear "saved"
        return self.snapshot()

    def wait_change(self, last_version: int, timeout: float) -> int:
        with self.condition:
            self.condition.wait_for(lambda: self.version != last_version or self._stop.is_set(), timeout=timeout)
            return self.version
