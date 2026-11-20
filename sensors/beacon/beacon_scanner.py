#!/usr/bin/env python3
"""ripples BLE 비콘 수신기 (2026-11-18) — 혈 Pi · 맥 Pi 공용. 사양: assets/2026-11/RASPBERRY_PI_HANDOFF.md

ESP32-C3 비콘(노드 1~5)의 광고를 BlueZ + Bleak 로 계속 받는다. 다른 프로그램(혈 ToF·천장 송신, 맥 마이크)과 따로 돈다.

- Service Data UUID bf46fcd8-… 의 10바이트만 본다: 버전 1 · group_id(uint16 LE) · hardware_id 6바이트 · 기준 RSSI(int8, 127=미보정)
- 노드 키 = (UUID, group_id). MAC 으로 거르지 않는다. DuplicateData=True 로 반복 관측.
- 같은 group 에 다른 hardware_id 가 3초 안에 보이면 CONFLICT (그 노드는 값 없이 제외, 두 장치 RSSI 를 섞지 않음)
- 3초 이상 못 받으면 STALE (값 없음 — 0 dBm 으로 채우지 않음)
- 원시 로그(수신 시각 · 수신기 ID · group · hardware_id · RSSI) 를 시간별 CSV 로 저장
- 1초마다 노드별 LAST · 최근 창(기본 1.5초) 중앙값 · age(ms) 를 집계 → run/beacon_latest.json, 요약 CSV, (설정 시) 전시컴으로 HTTP POST
- RSSI 를 거리로 바꾸지 않는다. 집계의 "nearest" 는 '가장 센 노드'와 2등과의 차(dB)일 뿐 — 가까운 구역 판별용.

설정 run/beacon.json (5초마다 다시 읽음, 재시작 불필요. 없으면 기본값):
  {"post_url": "", "receiver_id": "", "window_s": 1.5, "stale_s": 3.0, "conflict_s": 3.0, "zones": {"1": "입구"}}
  post_url 이 비면 보내지 않음. 예: "http://192.168.137.1:8000/api/beacon"

실행:  python3 beacon_scanner.py              (서비스로는 install.sh)
점검:  python3 beacon_scanner.py --selftest   (블루투스 없이 해석 · 집계만 시험)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import socket
import statistics
import struct
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

UUID = "bf46fcd8-bd8d-5695-a08b-a7c1d419be4e"
HERE = Path(__file__).resolve().parent
if (HERE / "vendor").is_dir():   # install.sh 가 wheels 를 풀어 둔 곳 (Pi 에 인터넷 없이 설치)
    sys.path.insert(0, str(HERE / "vendor"))
RUN = Path(os.environ.get("BEACON_RUN_DIR", HERE / "run"))
DATA = Path(os.environ.get("BEACON_DATA_DIR", Path.home() / "ripples-data" / "beacon"))
DEFAULT = {"post_url": "", "receiver_id": "", "window_s": 1.5, "stale_s": 3.0, "conflict_s": 3.0, "zones": {}}


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---- 해석 -----------------------------------------------------------------------------------------------------------
def decode(payload: bytes | None, rssi: int | None) -> dict | None:
    """사양의 decode 와 같음. 맞지 않으면 None."""
    if payload is None or len(payload) != 10 or rssi is None or rssi == 127:
        return None
    version, group, identity, reference = struct.unpack("<BH6sb", bytes(payload))
    if version != 1 or group == 0:
        return None
    return {"node": group, "rssi_dbm": int(rssi), "hardware_id": identity.hex().upper(),
            "reference_rssi_dbm": None if reference == 127 else reference}


# ---- 집계 -----------------------------------------------------------------------------------------------------------
class Nodes:
    """노드(group_id)별 최근 관측. t 는 time.monotonic() 초."""

    def __init__(self) -> None:
        self.obs: dict[int, deque] = {}          # node -> deque[(t, rssi, hwid, ref)]
        self.last_hw: dict[int, str] = {}

    def add(self, t: float, d: dict) -> None:
        q = self.obs.setdefault(d["node"], deque())
        if self.last_hw.get(d["node"]) not in (None, d["hardware_id"]):
            log(f"노드 {d['node']}: hardware_id 바뀜 {self.last_hw[d['node']]} → {d['hardware_id']}")
        self.last_hw[d["node"]] = d["hardware_id"]
        q.append((t, d["rssi_dbm"], d["hardware_id"], d["reference_rssi_dbm"]))

    def summary(self, now: float, cfg: dict) -> dict:
        win, stale, conf = float(cfg["window_s"]), float(cfg["stale_s"]), float(cfg["conflict_s"])
        keep = max(win, stale, conf) + 1.0
        nodes = {}
        for node, q in self.obs.items():
            while q and now - q[0][0] > keep:
                q.popleft()
            if not q:
                nodes[node] = {"state": "STALE", "age_ms": None}
                continue
            t_last, r_last, hw_last, ref = q[-1]
            age = int((now - t_last) * 1000)
            hws = sorted({o[2] for o in q if now - o[0] <= conf})
            if age >= stale * 1000:
                nodes[node] = {"state": "STALE", "age_ms": age, "hardware_id": hw_last}
            elif len(hws) > 1:
                nodes[node] = {"state": "CONFLICT", "age_ms": age, "hardware_ids": hws}
            else:
                recent = [o[1] for o in q if now - o[0] <= win]
                nodes[node] = {"state": "OK", "last_dbm": r_last,
                               "median_dbm": statistics.median(recent) if recent else None,
                               "n": len(recent), "age_ms": age, "hardware_id": hw_last,
                               "reference_rssi_dbm": ref}
        ok = sorted(((v["median_dbm"], k) for k, v in nodes.items() if v["state"] == "OK" and v["median_dbm"] is not None),
                    reverse=True)
        nearest = None
        if ok:
            zones = cfg.get("zones") or {}
            nearest = {"node": ok[0][1], "zone": zones.get(str(ok[0][1])), "median_dbm": ok[0][0],
                       "margin_db": round(ok[0][0] - ok[1][0], 1) if len(ok) > 1 else None}
        return {"nodes": {str(k): v for k, v in sorted(nodes.items())}, "nearest": nearest}


# ---- 저장 · 설정 · 보내기 ----------------------------------------------------------------------------------------------
class HourlyCsv:
    def __init__(self, stem: str, cols: list[str]) -> None:
        self.stem, self.cols, self.key, self.f, self.w = stem, cols, None, None, None

    def write(self, row: list) -> None:
        key = time.strftime("%Y-%m-%d/%H")
        if key != self.key:
            if self.f:
                self.f.close()
            day = DATA / time.strftime("%Y-%m-%d"); day.mkdir(parents=True, exist_ok=True)
            p = day / f"{self.stem}_{time.strftime('%Y%m%d_%H')}.csv"
            new = not p.exists()
            self.f = open(p, "a", newline="", buffering=1); self.w = csv.writer(self.f)
            if new:
                self.w.writerow(self.cols)
            self.key = key
        self.w.writerow(row)


class Config:
    def __init__(self) -> None:
        self.path, self.mt, self.at, self.v = RUN / "beacon.json", None, -1e9, dict(DEFAULT)

    def get(self) -> dict:
        if time.monotonic() - self.at > 5:
            self.at = time.monotonic()
            try:
                mt = self.path.stat().st_mtime
                if mt != self.mt:
                    self.mt, self.v = mt, dict(DEFAULT, **json.loads(self.path.read_text(encoding="utf-8")))
                    log("설정:", self.v)
            except FileNotFoundError:
                self.v = dict(DEFAULT)
            except (OSError, ValueError) as exc:
                log("설정 읽기 실패:", exc)
        return self.v


def post(url: str, body: dict) -> str | None:
    try:
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=0.8).read()
        return None
    except Exception as exc:   # 전시컴이 없거나 꺼져 있어도 수신 · 저장은 계속
        return str(exc)


# ---- 실행 -----------------------------------------------------------------------------------------------------------
async def run() -> None:
    from bleak import BleakScanner   # 여기서만 (selftest 는 bleak 없이)

    cfg = Config()
    receiver = cfg.get().get("receiver_id") or socket.gethostname()
    nodes = Nodes()
    raw = HourlyCsv("beacon_raw", ["t_wall_ns", "t_mono_ns", "receiver", "group_id", "hardware_id", "rssi_dbm", "reference_rssi_dbm"])
    agg = HourlyCsv("beacon_1s", ["t_wall_ns", "receiver", "node", "state", "last_dbm", "median_dbm", "n", "age_ms", "hardware_id"])
    RUN.mkdir(parents=True, exist_ok=True)
    count = {"cb": 0, "ours": 0, "last_ours": time.monotonic()}

    def on_adv(device, adv) -> None:
        count["cb"] += 1
        d = decode(adv.service_data.get(UUID), adv.rssi)
        if d is None:
            return
        t = time.monotonic(); count["ours"] += 1; count["last_ours"] = t
        nodes.add(t, d)
        raw.write([time.time_ns(), time.monotonic_ns(), receiver, d["node"], d["hardware_id"], d["rssi_dbm"],
                   "" if d["reference_rssi_dbm"] is None else d["reference_rssi_dbm"]])

    seq, post_err, stat_at = 0, None, time.monotonic()
    while True:
        scanner = BleakScanner(on_adv, scanning_mode="active",
                               bluez={"filters": {"Transport": "le", "DuplicateData": True}})
        try:
            await scanner.start()
            log(f"스캔 시작 · 수신기 {receiver} · UUID {UUID}")
            started = time.monotonic()
            while True:
                await asyncio.sleep(1.0)
                c = cfg.get(); now = time.monotonic(); seq += 1
                s = nodes.summary(now, c)
                body = {"v": 1, "type": "beacon", "receiver": receiver, "seq": seq, "t_wall_ns": time.time_ns(),
                        "window_s": c["window_s"], **s}
                tmp = RUN / "beacon_latest.json.tmp"
                tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8"); tmp.replace(RUN / "beacon_latest.json")
                for k, v in s["nodes"].items():
                    agg.write([body["t_wall_ns"], receiver, k, v["state"], v.get("last_dbm", ""), v.get("median_dbm", ""),
                               v.get("n", ""), "" if v.get("age_ms") is None else v["age_ms"],
                               v.get("hardware_id") or "|".join(v.get("hardware_ids", []))])
                if c.get("post_url"):
                    err = await asyncio.get_running_loop().run_in_executor(None, post, c["post_url"], body)
                    if err != post_err:
                        log("보내기:", "됨 → " + c["post_url"] if err is None else f"안 됨 ({err})"); post_err = err
                if now - stat_at >= 30:
                    states = " ".join(f"{k}:{v['state']}" for k, v in s["nodes"].items()) or "없음"
                    log(f"30초: 광고 {count['cb']} · 우리 비콘 {count['ours']} · {states}")
                    count["cb"] = count["ours"] = 0; stat_at = now
                # BlueZ 가 가끔 보고를 멈춤: 받던 비콘이 60초 동안 하나도 안 오면 스캐너를 다시 켬
                if now - count["last_ours"] > 60 and now - started > 60:
                    log("60초 동안 비콘 없음 → 스캐너 다시 시작"); count["last_ours"] = now
                    break
        except Exception as exc:
            log("스캐너 오류:", repr(exc), "→ 3초 뒤 다시")
        finally:
            try:
                await scanner.stop()
            except Exception:
                pass
        await asyncio.sleep(3.0)


def selftest() -> None:
    p = bytes.fromhex("010300AABBCCDDEEFF7F")
    d = decode(p, -60); assert d == {"node": 3, "rssi_dbm": -60, "hardware_id": "AABBCCDDEEFF", "reference_rssi_dbm": None}, d
    assert decode(p, 127) is None and decode(p[:9], -60) is None and decode(bytes.fromhex("020300AABBCCDDEEFF7F"), -60) is None
    assert decode(bytes.fromhex("010000AABBCCDDEEFF7F"), -60) is None
    assert decode(bytes.fromhex("010100AABBCCDDEEFFC4"), -50)["reference_rssi_dbm"] == -60
    n, cfg = Nodes(), dict(DEFAULT, zones={"1": "입구"})
    for i in range(20):
        n.add(100 + i * 0.1, {"node": 1, "rssi_dbm": -50 - (i % 3), "hardware_id": "A1", "reference_rssi_dbm": None})
        n.add(100 + i * 0.1, {"node": 2, "rssi_dbm": -70, "hardware_id": "B1", "reference_rssi_dbm": None})
    n.add(101.0, {"node": 3, "rssi_dbm": -60, "hardware_id": "C1", "reference_rssi_dbm": None})
    n.add(101.5, {"node": 3, "rssi_dbm": -61, "hardware_id": "C2", "reference_rssi_dbm": None})   # 같은 그룹 다른 장치
    n.add(95.0, {"node": 4, "rssi_dbm": -40, "hardware_id": "D1", "reference_rssi_dbm": None})    # 오래 전
    s = n.summary(102.0, cfg)
    assert s["nodes"]["1"]["state"] == "OK" and s["nodes"]["1"]["median_dbm"] == -51, s["nodes"]["1"]
    assert s["nodes"]["3"]["state"] == "CONFLICT" and "median_dbm" not in s["nodes"]["3"]
    assert s["nodes"]["4"]["state"] == "STALE" and "median_dbm" not in s["nodes"]["4"]
    assert s["nearest"] == {"node": 1, "zone": "입구", "median_dbm": -51, "margin_db": 19}, s["nearest"]
    print("selftest ok"); print(json.dumps(s, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    if ap.parse_args().selftest:
        selftest(); sys.exit(0)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
