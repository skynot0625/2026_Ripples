"""Homogeneous (non-E/I) sparse Gaussian weights, compatible with a reference reservoir.

Reproduces the reference implementation draw-for-draw (one
``default_rng(seed)`` stream: W_in uniform, then ``scipy.sparse.random`` with
N(0,1) values, zero diagonal, ARPACK radius estimate with a random ``v0`` and
``tol=1e-4``, rescale).  The ARPACK estimate is kept for bit-compatibility; the
dense ("true") radius is reported separately because ARPACK under-reports it
(e.g. 0.8908 stored vs 0.9000 true at N=1000, seed 42).
Not used by the biological E/I reservoir.
"""

from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
from scipy import sparse
from scipy.sparse import linalg as sparse_linalg


def _arpack_radius(matrix, rng, n) -> float:
    if matrix.nnz == 0:
        return 0.0
    if n == 1:
        return abs(float(matrix[0, 0]))
    if n <= 128:
        return float(np.max(np.abs(np.linalg.eigvals(matrix.toarray()))))
    value = sparse_linalg.eigs(matrix, k=1, which="LM", return_eigenvectors=False,
                               v0=rng.standard_normal(n), tol=1e-4, maxiter=max(1000, 5 * n))[0]
    return float(abs(value))


def sparse_gaussian_chaos_compatible(
    n: int, n_inputs: int, density: float = 0.05, spectral_radius: float = 0.9,
    input_scale: float = 0.5, seed: int = 42,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, float]]:
    W_in, W, info = sparse_gaussian_chaos_compatible_csr(n, n_inputs, density, spectral_radius, input_scale, seed)
    return W_in, W.toarray(), info


def sparse_gaussian_chaos_compatible_csr(
    n: int, n_inputs: int, density: float = 0.05, spectral_radius: float = 0.9,
    input_scale: float = 0.5, seed: int = 42,
) -> Tuple[np.ndarray, sparse.csr_matrix, Dict[str, float]]:
    """Same draws, but ``W_rec`` is returned as the exact CSR matrix of the reference implementation.

    Keeping its (unsorted) CSR index order keeps the ``W @ s`` summation order,
    and therefore every reservoir state, bit-identical to the reference.
    """

    rng = np.random.default_rng(seed)
    W_in = rng.uniform(-input_scale, input_scale, size=(n, n_inputs))
    W = sparse.random(n, n, density=density, format="csr", random_state=rng,
                      data_rvs=lambda size: rng.standard_normal(size), dtype=float)
    W.setdiag(0.0)
    W.eliminate_zeros()
    radius = _arpack_radius(W, rng, n)
    if spectral_radius == 0.0:
        W.data.fill(0.0); W.eliminate_zeros()
    else:
        W = W * (spectral_radius / radius)
    W = W.tocsr()
    eff = 0.0 if spectral_radius == 0.0 else _arpack_radius(W, rng, n)
    dense = W.toarray()
    info = {
        "initial_radius_arpack": radius,
        "effective_radius_arpack": eff,
        "rho_true_dense": float(np.max(np.abs(np.linalg.eigvals(dense)))) if dense.any() else 0.0,
    }
    return W_in, W, info
