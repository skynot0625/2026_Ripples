"""Reservoir fingerprints and run-environment provenance."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import numpy as np
import torch

from .config import to_dict

REPO_ROOT = Path(__file__).resolve().parents[2]


def array_hash(*arrays: Any) -> str:
    h = hashlib.sha256()
    for a in arrays:
        a = a.detach().cpu().numpy() if isinstance(a, torch.Tensor) else np.asarray(a)
        a = np.ascontiguousarray(a.astype(np.float64) if a.dtype.kind == "f" else a)
        h.update(str(a.shape).encode()); h.update(str(a.dtype).encode()); h.update(a.tobytes())
    return h.hexdigest()[:16]


def git_commit() -> Dict[str, Any]:
    def run(*args):
        try:
            return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:  # pragma: no cover
            return ""
    return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain", "--", "src"))}


def environment() -> Dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "numpy": np.__version__,
        "torch": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "command": " ".join(sys.argv),
        "git": git_commit(),
    }


def _rho(W: np.ndarray) -> float:
    return float(np.max(np.abs(np.linalg.eigvals(W)))) if W.any() else 0.0


def reservoir_fingerprint(res, compute_rho: bool = True) -> Dict[str, Any]:
    """Identity and structure record.  ``compute_rho=False`` skips the two dense eigensolves
    (``rho_stored`` / ``rho_applied`` become ``None``); every identity hash is unaffected.
    Frozen-artifact task runs use it so that no recurrent eigensolve runs on the task path."""

    from .connectivity import dale_violations, scaling_record

    cfg = res.cfg
    W = res.W.detach().cpu().numpy().astype(np.float64)
    Wa = res.W_applied.detach().cpu().numpy().astype(np.float64)
    NE = res.N_E
    blocks = {"E_to_E": (slice(0, NE), slice(0, NE)), "E_to_I": (slice(NE, None), slice(0, NE)),
              "I_to_E": (slice(0, NE), slice(NE, None)), "I_to_I": (slice(NE, None), slice(NE, None))}
    block_stats = {}
    for name, (r, c) in blocks.items():
        B = W[r, c]
        nz = B[B != 0]
        block_stats[name] = {"shape": list(B.shape), "nnz": int(nz.size),
                             "density": float(nz.size / max(B.size, 1)),
                             "mean_abs_weight": float(np.abs(nz).mean()) if nz.size else 0.0,
                             "sign": float(np.sign(nz).mean()) if nz.size else 0.0}
    b = res.neurons.b.cpu().numpy()
    tw = res.neurons.tau_w_ms.cpu().numpy()
    delays = res.delay_steps.cpu().numpy()
    structure_hash = array_hash(W != 0, res.neurons.tau_m_ms, tw, b, delays)
    base_hash = array_hash(res.structure.W0) if res.structure is not None else structure_hash
    rid = array_hash(Wa, res.W_in, res.neurons.tau_m_ms, tw, b, delays, res.neurons.threshold,
                     res.neurons.refractory_steps)
    scal = (scaling_record(res.structure, cfg.scaling) if res.structure is not None
            else {"mode": cfg.scaling.mode, "note": "built from explicit arrays"})
    weak = np.isclose(b[:NE], cfg.adaptation.weak.b)
    return {
        "reservoir_id": rid,
        "base_structure_id": base_hash,
        "config_name": cfg.name,
        "family": cfg.family,
        "semantics": cfg.semantics_label,
        "legacy_flags": to_dict(cfg.legacy),
        "N_E": NE, "N_I": res.N_I, "dt_ms": cfg.dt_ms,
        "seeds_resolved": res.seeds,
        "scaling": scal,
        "rho_stored": _rho(W) if compute_rho else None,
        "rho_applied": _rho(Wa) if compute_rho else None,
        "rho_source": "dense eigensolve" if compute_rho else "not recomputed (frozen artifact; see structure rho0)",
        "dale_violations": dale_violations(W, NE),
        "blocks": block_stats,
        "self_connections": int(np.count_nonzero(np.diag(W))),
        "hash_W_stored": array_hash(W), "hash_W_applied": array_hash(Wa), "hash_W_in": array_hash(res.W_in),
        "adaptation": {"preset": cfg.adaptation.name, "n_weak_E": int(weak.sum()), "n_strong_E": int((~weak).sum()),
                       "b_values_E": sorted({float(x) for x in np.round(b[:NE], 6)}),
                       "b_values_I": sorted({float(x) for x in np.round(b[NE:], 6)}),
                       "tau_w_median_weak": float(np.median(tw[:NE][weak])) if weak.any() else None,
                       "tau_w_median_strong": float(np.median(tw[:NE][~weak])) if (~weak).any() else None},
        "delay_steps_histogram": np.bincount(delays).tolist(),
        "max_delay_steps": int(res.max_delay_steps),
        "refractory_steps_E_I": [int(res.neurons.refractory_steps[0]), int(res.neurons.refractory_steps[-1])],
        "noise": to_dict(cfg.noise),
        "n_inputs": int(res.W_in.shape[1]),
        "code": git_commit(),
    }


def write_json(obj: Any, path: Path) -> None:
    Path(path).write_text(json.dumps(obj, indent=2, default=_json_default))


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.bool_,)):
        return bool(o)
    raise TypeError(type(o))
