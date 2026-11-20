"""Deterministic parallel sweep runner.

Layout of ``out_dir``::

    runs/<condition_id>/            finalised runs only (each contains COMPLETED.json)
    .tmp/<condition_id>.a<k>.<uid>/ in-progress attempt (private to one worker process)
    failed/<condition_id>.a<k>/     failed attempt directories (kept for inspection)
    stale/                          leftover .tmp dirs from an interrupted runner
    invalid/                        finalised dirs that failed resume validation
    attempts/<condition_id>.a<k>.json   exit status, timing, stdout/stderr paths of every attempt
    sweep_status.json

Guarantees:
* one OS process per condition (``python -m snnrc.sweep.worker``), OMP/MKL/torch
  threads = 1, no shared RNG state (all streams come from named seeds in the config);
* a run appears under ``runs/`` only after a zero exit status AND the presence of
  the required artifacts; finalisation = write ``COMPLETED.json`` then one
  ``os.rename`` of the attempt directory (atomic on one filesystem);
* resume skips a condition only if ``runs/<id>/COMPLETED.json`` is valid: same
  manifest SHA-256, config.yaml hash and content identical to the freshly built
  config, reservoir_fingerprint.json hash unchanged, all required files present;
* failed conditions are recorded and can be retried individually (``--only``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..config import load_config, to_dict, AvalancheRunConfig
from .manifest import (Condition, build_run_config, enumerate_conditions, execution_order, load_manifest,
                       manifest_hash)

REQUIRED_FILES = ("config.yaml", "manifest.json", "reservoir_fingerprint.json", "spike_events.npz",
                  "population_activity.npz", "activity_metrics.json", "kick_events.csv", "safety_events.csv",
                  "timing.json", "stage3_5/binned_summary.json")
SRC_DIR = Path(__file__).resolve().parents[2]


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _config_dict(m: Dict, cond: Condition, duration_ms: Optional[float]) -> Dict:
    return to_dict(build_run_config(m, cond, duration_ms))


def validate_completed(run_dir: Path, m: Dict, m_hash: str, cond: Condition, duration_ms: Optional[float]) -> Optional[str]:
    """Return None if the finalised run is valid for resume, else the reason."""

    marker = run_dir / "COMPLETED.json"
    if not marker.exists():
        return "missing COMPLETED.json"
    try:
        c = json.loads(marker.read_text())
    except Exception:  # noqa: BLE001
        return "unreadable COMPLETED.json"
    if c.get("manifest_sha256") != m_hash:
        return "manifest hash mismatch"
    for f in REQUIRED_FILES:
        if not (run_dir / f).exists():
            return f"missing {f}"
    if _sha(run_dir / "config.yaml") != c.get("config_sha256"):
        return "config.yaml changed"
    if to_dict(load_config(AvalancheRunConfig, run_dir / "config.yaml")) != _config_dict(m, cond, duration_ms):
        return "config differs from manifest-built config"
    if _sha(run_dir / "reservoir_fingerprint.json") != c.get("fingerprint_sha256"):
        return "reservoir fingerprint changed"
    fp = json.loads((run_dir / "reservoir_fingerprint.json").read_text())
    if fp.get("reservoir_id") != c.get("reservoir_id"):
        return "reservoir_id mismatch"
    return None


def _worker_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.update(PYTHONPATH=str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", ""), PYTHONDONTWRITEBYTECODE="1",
               OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1")
    return env


def _next_attempt(out: Path, cid: str) -> int:
    return len(list((out / "attempts").glob(f"{cid}.a*.json"))) + 1


def run_one(out: Path, manifest_path: Path, m: Dict, m_hash: str, cond: Condition, duration_ms: Optional[float],
            mode: str) -> Dict:
    cid = cond.condition_id
    k = _next_attempt(out, cid)
    tmp = out / ".tmp" / f"{cid}.a{k}.{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True)
    cmd = [sys.executable, "-m", "snnrc.sweep.worker", "--manifest", str(manifest_path), "--family", cond.family,
           "--g", repr(cond.g_global), "--seed", str(cond.seed), "--run-dir", str(tmp)]
    if duration_ms is not None:
        cmd += ["--duration-ms", repr(float(duration_ms))]
    if cond.J_I is not None:
        cmd += ["--j-i", repr(float(cond.J_I))]
    t0 = time.time()
    with open(tmp / "stdout.log", "w") as so, open(tmp / "stderr.log", "w") as se:
        rc = subprocess.call(cmd, stdout=so, stderr=se, env=_worker_env())
    wall = time.time() - t0
    rec = {"condition_id": cid, "attempt": k, "exit_status": rc, "wall_s": wall, "command": cmd, "mode": mode,
           "started_unix": t0}
    missing = [f for f in REQUIRED_FILES if not (tmp / f).exists()]
    final = out / "runs" / cid
    if rc == 0 and not missing:
        fp = json.loads((tmp / "reservoir_fingerprint.json").read_text())
        marker = {"condition_id": cid, "family": cond.family, "g_global": cond.g_global, "seed": cond.seed,
                  "control_parameter": {"g": "g_global", "JE": "J_E_exc", "JE_JI": "J_E_exc"}[cond.param],
                  "control_value": cond.g_global, "J_I": cond.J_I,
                  "mode": mode, "duration_ms": load_config(AvalancheRunConfig, tmp / "config.yaml").duration_ms,
                  "manifest_path": str(manifest_path), "manifest_sha256": m_hash,
                  "config_sha256": _sha(tmp / "config.yaml"),
                  "fingerprint_sha256": _sha(tmp / "reservoir_fingerprint.json"),
                  "reservoir_id": fp["reservoir_id"], "base_structure_id": fp["base_structure_id"],
                  "run_status": json.loads((tmp / "activity_metrics.json").read_text())["run_status"],
                  "attempt": k, "finalised_unix": time.time()}
        with open(tmp / "COMPLETED.json", "w") as fh:
            json.dump(marker, fh, indent=2)
            fh.flush(); os.fsync(fh.fileno())
        if final.exists():                       # should not happen (validated before launch)
            shutil.move(str(final), str(out / "invalid" / f"{cid}.{int(time.time())}"))
        os.rename(tmp, final)                    # atomic finalisation
        rec.update(status="completed", run_dir=str(final), run_status=marker["run_status"])
    else:
        dest = out / "failed" / f"{cid}.a{k}"
        shutil.move(str(tmp), str(dest))
        rec.update(status="failed", failed_dir=str(dest), missing_files=missing,
                   stderr_tail=(dest / "stderr.log").read_text()[-2000:])
    (out / "attempts" / f"{cid}.a{k}.json").write_text(json.dumps(rec, indent=2))
    return rec


def run_sweep(manifest_path: Path, out_dir: Path, max_workers: Optional[int] = None,
              families: Optional[Sequence[str]] = None, g_values: Optional[Sequence[float]] = None,
              seeds: Optional[Sequence[int]] = None, only: Optional[Sequence[str]] = None,
              duration_ms: Optional[float] = None, mode: str = "full",
              J_I_values: Optional[Sequence[float]] = None) -> Dict:
    manifest_path = Path(manifest_path).resolve()
    m = load_manifest(manifest_path)
    if mode == "pilot":
        if not str(m["status"]).startswith("APPROVED_EXPLORATORY_PILOT"):
            raise SystemExit("pilot mode requires an APPROVED_EXPLORATORY_PILOT manifest")
        if duration_ms is not None:
            raise SystemExit("pilot mode uses the manifest duration; no override")
    if mode == "full":
        if m["status"] != "APPROVED_PREREGISTERED":
            raise SystemExit("full sweep requires an APPROVED_PREREGISTERED manifest")
        if duration_ms is not None:
            raise SystemExit("duration override is only allowed in preflight/test modes")
    m_hash = manifest_hash(manifest_path)
    out = Path(out_dir).resolve()
    for sub in ("runs", ".tmp", "failed", "stale", "invalid", "attempts"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for leftover in (out / ".tmp").iterdir():   # interrupted attempts never count as completed
        shutil.move(str(leftover), str(out / "stale" / leftover.name))
    conds = enumerate_conditions(m, families, g_values, seeds, J_I_values)
    if only:
        conds = [c for c in conds if c.condition_id in set(only)]
    order = execution_order(conds, m.get("execution", {}).get("order_seed", 0))
    todo, skipped, invalid = [], [], {}
    for c in order:
        final = out / "runs" / c.condition_id
        if final.exists():
            why = validate_completed(final, m, m_hash, c, duration_ms)
            if why is None:
                skipped.append(c.condition_id)
                continue
            invalid[c.condition_id] = why
            shutil.move(str(final), str(out / "invalid" / f"{c.condition_id}.{int(time.time())}"))
        todo.append(c)
    workers = int(max_workers or m.get("execution", {}).get("default_max_workers", 20))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        records = list(ex.map(lambda c: run_one(out, manifest_path, m, m_hash, c, duration_ms, mode), todo))
    status = {"manifest": str(manifest_path), "manifest_sha256": m_hash, "mode": mode, "duration_override_ms": duration_ms,
              "max_workers": workers, "n_conditions": len(conds), "skipped_valid": skipped,
              "invalidated": invalid, "attempted": [r["condition_id"] for r in records],
              "completed": [r["condition_id"] for r in records if r["status"] == "completed"],
              "failed": [r["condition_id"] for r in records if r["status"] == "failed"],
              "wall_s": time.time() - t0}
    (out / "sweep_status.json").write_text(json.dumps(status, indent=2))
    return status


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Stage 4 sweep runner")
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--max-workers", type=int, default=None)
    ap.add_argument("--families", nargs="*", default=None)
    ap.add_argument("--g", nargs="*", type=float, default=None)
    ap.add_argument("--seeds", nargs="*", type=int, default=None)
    ap.add_argument("--j-i", nargs="*", type=float, default=None, help="J_I subset (2D grids only)")
    ap.add_argument("--only", nargs="*", default=None, help="condition ids to (re)run, e.g. B0_g07.00_s0")
    ap.add_argument("--duration-ms", type=float, default=None, help="preflight/test only")
    ap.add_argument("--mode", choices=["full", "pilot", "preflight", "test"], default="full")
    a = ap.parse_args(argv)
    st = run_sweep(a.manifest, a.out, a.max_workers, a.families, a.g, a.seeds, a.only, a.duration_ms, a.mode, a.j_i)
    print(json.dumps({k: v for k, v in st.items() if k != "skipped_valid"}, indent=1))
    if st["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
