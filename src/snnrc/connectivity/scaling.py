"""The two recurrent-strength manipulations.  Never compare their values as if equal.

``global`` (historical parameterisation, there called ``J_E_exc`` / ``J_E``)::

    W = g_global * W0 / rho(W0)          =>  rho(W) = g_global exactly

All four blocks scale together; the I/E magnitude ratio stays fixed.

``excitatory`` (true excitatory-strength manipulation)::

    W[:, E] = J_E_exc * W0[:, E] / rho(W0)
    W[:, I] = (J_I / r) * W0[:, I] / rho(W0)        r = inh_to_exc_magnitude_ratio

so an E-presynaptic weight equals ``J_E_exc / rho(W0)`` and an I->E weight equals
``-J_I / rho(W0)`` (I->I additionally times ``ii_attenuation``).  ``rho(W0)`` is
a fixed per-structure normaliser (not recomputed per condition).  The only point
where both parameterisations coincide is ``J_E_exc = g``, ``J_I = r * g``.
"""

from __future__ import annotations

import numpy as np

from ..config import ScalingConfig
from .dale_ei import DaleEIStructure


def apply_scaling(structure: DaleEIStructure, scaling: ScalingConfig,
                  inh_to_exc_magnitude_ratio: float = 4.0) -> np.ndarray:
    W0, NE, rho0 = structure.W0, structure.N_E, structure.rho0
    if rho0 <= 0:
        raise ValueError("base matrix has zero spectral radius")
    if scaling.mode == "global":
        return W0 * (scaling.g_global / rho0)
    if scaling.mode == "excitatory":
        W = np.empty_like(W0)
        W[:, :NE] = W0[:, :NE] * (scaling.J_E_exc / rho0)
        W[:, NE:] = W0[:, NE:] * (scaling.J_I / inh_to_exc_magnitude_ratio / rho0)
        return W
    raise ValueError(f"unknown scaling mode {scaling.mode!r}")


def scaling_record(structure: DaleEIStructure, scaling: ScalingConfig) -> dict:
    rec = {"mode": scaling.mode, "rho_W0": structure.rho0}
    if scaling.mode == "global":
        rec.update(g_global=scaling.g_global, alias_historical="J_E (NPRC slides) / J_E_exc (legacy code)")
    else:
        rec.update(J_E_exc=scaling.J_E_exc, J_I=scaling.J_I,
                   E_weight=scaling.J_E_exc / structure.rho0, I_to_E_weight=-scaling.J_I / structure.rho0)
    return rec
