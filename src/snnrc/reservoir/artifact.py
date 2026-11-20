"""Canonical frozen reservoir artifacts.

A reservoir is saved as the exact arrays that define it, so a frozen reservoir never
has to be rebuilt from an eigensolve (``rho(W0)`` from the dense eigensolve depends on
the BLAS thread count in its last digits).

Directory layout::

    <dir>/arrays.npz    W (stored, [post, pre]), W_applied, W_in, tau_m, tau_w, b, delay_steps,
                        threshold, v_reset, refractory_steps, subtract_reset, is_weak_E,
                        structure_W0, structure_mask, structure_coords, structure_fitness,
                        structure_distance, structure_rho0
    <dir>/meta.json     ReservoirConfig, resolved seeds, dtype, construction metadata,
                        content SHA-256, reservoir_id / structure hashes

Identity:

* ``artifact_sha256`` = sha256 over the arrays in sorted name order (name, dtype, shape,
  raw bytes). It is thread-independent and verified on every load.
* ``reservoir_id`` (``snnrc.provenance.reservoir_fingerprint``) is an array hash too, and
  is re-checked after loading.

``load_reservoir_artifact(dir, n_inputs=k)`` attaches a task-specific input projection
drawn exactly as a config build would (``sample_input_weights`` with the reservoir's
``input_weights`` seed). The recurrent structure is the frozen one.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import torch

from ..config import ReservoirConfig, from_dict, to_dict
from ..connectivity.dale_ei import DaleEIStructure

FORMAT_VERSION = "snnrc-reservoir-artifact/1"
_STRUCT = ("W0", "mask", "coords", "fitness", "distance")


def _np(t) -> np.ndarray:
    return t.detach().cpu().numpy() if isinstance(t, torch.Tensor) else np.asarray(t)


def content_sha256(arrays: Dict[str, np.ndarray]) -> str:
    h = hashlib.sha256()
    for name in sorted(arrays):
        a = np.ascontiguousarray(arrays[name])
        h.update(name.encode()); h.update(str(a.dtype).encode()); h.update(str(a.shape).encode()); h.update(a.tobytes())
    return h.hexdigest()


def reservoir_arrays(res) -> Dict[str, np.ndarray]:
    n = res.neurons
    out = {"W": _np(res.W), "W_applied": _np(res.W_applied), "W_in": _np(res.W_in), "tau_m": _np(n.tau_m_ms),
           "tau_w": _np(n.tau_w_ms), "b": _np(n.b), "delay_steps": _np(res.delay_steps),
           "max_delay_steps": np.array(res.max_delay_steps), "threshold": _np(n.threshold), "v_reset": _np(n.v_reset),
           "refractory_steps": _np(n.refractory_steps), "subtract_reset": _np(n.subtract_reset),
           "is_weak_E": np.asarray(res.is_weak_E if res.is_weak_E is not None else np.zeros(0, bool))}
    if res.structure is not None:
        s = res.structure
        out.update({f"structure_{k}": np.asarray(getattr(s, k)) for k in _STRUCT})
        out["structure_rho0"] = np.array(s.rho0, dtype=np.float64)
        out["structure_topology_seed"] = np.array(s.topology_seed, dtype=np.int64)
    return out


def save_reservoir_artifact(res, directory, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Write ``arrays.npz`` + ``meta.json``; return the meta record."""

    from ..provenance import git_commit

    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    arrays = reservoir_arrays(res)
    fp = res.fingerprint()
    meta = {
        "format": FORMAT_VERSION,
        "config": to_dict(res.cfg), "seeds_resolved": res.seeds, "dtype": res.cfg.dtype,
        "n_inputs": int(arrays["W_in"].shape[1]),
        "artifact_sha256": content_sha256(arrays),
        "identity": {k: fp[k] for k in ("reservoir_id", "base_structure_id", "hash_W_stored", "hash_W_applied", "hash_W_in")},
        "scaling": fp.get("scaling"),
        "rho0_stored_in_artifact": float(arrays["structure_rho0"]) if "structure_rho0" in arrays else None,
        "construction": {
            "threads": {v: os.environ.get(v) for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")},
            "torch_threads": torch.get_num_threads(), "numpy": np.__version__, "torch": torch.__version__,
            "python": platform.python_version(), "code": git_commit()},
        **(extra or {}),
    }
    np.savez_compressed(d / "arrays.npz", **arrays)
    (d / "meta.json").write_text(json.dumps(meta, indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))
    return meta


def load_reservoir_artifact(directory, n_inputs: int = 0, device: str = "cpu", verify: bool = True,
                            expected_artifact_sha256: Optional[str] = None,
                            expected_reservoir_id: Optional[str] = None):
    """Rebuild the reservoir from its saved arrays (no eigensolve); verify its identity.

    ``expected_*`` (frozen scientific configs) must equal the artifact's content hash and its
    task-free ``reservoir_id`` (recomputed from the saved arrays, independent of ``n_inputs``)."""

    from . import BiologicalEIReservoir
    from .biological_ei import sample_input_weights

    d = Path(directory)
    meta = json.loads((d / "meta.json").read_text())
    if meta.get("format") != FORMAT_VERSION:
        raise ValueError(f"unknown reservoir artifact format {meta.get('format')!r}")
    with np.load(d / "arrays.npz", allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files}
    sha = content_sha256(arrays)
    if verify and sha != meta["artifact_sha256"]:
        raise ValueError(f"{d}: artifact arrays do not match their recorded SHA-256")
    if expected_artifact_sha256 is not None and sha != expected_artifact_sha256:
        raise ValueError(f"{d}: artifact SHA-256 {sha} != expected {expected_artifact_sha256}")
    cfg = from_dict(ReservoirConfig, meta["config"])
    W_in = arrays["W_in"]
    if n_inputs:
        if W_in.shape[1] not in (0, n_inputs):
            raise ValueError(f"artifact has {W_in.shape[1]} inputs, {n_inputs} requested")
        if W_in.shape[1] == 0:
            cfg = copy.deepcopy(cfg)
            cfg.input.n_inputs = int(n_inputs)
            W_in = sample_input_weights(cfg, int(meta["seeds_resolved"]["input_weights"]))
    res = BiologicalEIReservoir.from_arrays(cfg, arrays["W"], W_in, arrays["tau_m"], arrays["tau_w"], arrays["b"],
                                            arrays["delay_steps"], device=device)
    if "structure_W0" in arrays:
        res.structure = DaleEIStructure(cfg.N_E, cfg.N_I, arrays["structure_W0"], arrays["structure_mask"],
                                        arrays["structure_coords"], arrays["structure_fitness"],
                                        arrays["structure_distance"], int(arrays["structure_topology_seed"]),
                                        float(arrays["structure_rho0"]))
    res.is_weak_E = arrays["is_weak_E"] if arrays["is_weak_E"].size else None
    res.artifact_meta = meta
    if verify:
        _np_eq = lambda a, b: np.array_equal(_np(a), b)   # noqa: E731
        for name, t in (("W_applied", res.W_applied), ("threshold", res.neurons.threshold), ("v_reset", res.neurons.v_reset),
                        ("refractory_steps", res.neurons.refractory_steps), ("subtract_reset", res.neurons.subtract_reset)):
            if not _np_eq(t, arrays[name]):
                raise ValueError(f"{d}: rebuilt {name} differs from the artifact")
        if res.max_delay_steps != int(arrays["max_delay_steps"]):
            raise ValueError(f"{d}: rebuilt max delay differs from the artifact")
        from ..provenance import array_hash

        # task-free identity, from the rebuilt reservoir with the artifact's own (stored) input projection
        got = array_hash(_np(res.W_applied), arrays["W_in"], res.neurons.tau_m_ms, res.neurons.tau_w_ms, res.neurons.b,
                         res.delay_steps, res.neurons.threshold, res.neurons.refractory_steps)
        if got != meta["identity"]["reservoir_id"]:
            raise ValueError(f"{d}: rebuilt reservoir_id {got} != artifact {meta['identity']['reservoir_id']}")
        if expected_reservoir_id is not None and got != expected_reservoir_id:
            raise ValueError(f"{d}: reservoir_id {got} != expected {expected_reservoir_id}")
    return res
