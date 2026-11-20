"""Corrected Dale E/I connectivity generator (one convention: ``W[post, pre]``).

Same fitness + spatial construction as the historical E/I models, with their
defects removed:

* one orientation for every block (legacy E->E was applied transposed);
* independent ``topology`` seed (legacy re-seeded the global ``np.random(42)``);
* signs are attached per presynaptic column (Dale), so there is no place where
  an inhibitory sign could be applied twice.

Connection probability (self-connections excluded)::

    p[post, pre] = p_scale * f_pre * (f_post if pop(post) == pop(pre) else 1)
                   * exp(-|x_post - x_pre| / lambda_{pop(pre)})

which reproduces the historical block formulas (EE: f_i f_j; E->I and I->E:
presynaptic fitness only; II: f_i f_j).  Coordinates are uniform in [0, 1]^2;
fitness is ``Pareto(alpha) + 1`` normalised to mean 1 per population.

Base magnitudes ``W0``:
    E-presynaptic columns:  +1
    I->E:                   -inh_to_exc_magnitude_ratio
    I->I:                   -inh_to_exc_magnitude_ratio * ii_attenuation
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import ReservoirConfig
from ..seeding import numpy_rng, resolve_seeds


@dataclass
class DaleEIStructure:
    N_E: int
    N_I: int
    W0: np.ndarray            # (N, N) signed base matrix, [post, pre]
    mask: np.ndarray          # (N, N) bool
    coords: np.ndarray        # (N, 2)
    fitness: np.ndarray       # (N,)
    distance: np.ndarray      # (N, N), distance[post, pre]
    topology_seed: int
    rho0: float               # spectral radius of W0 (dense eigvals)

    @property
    def N(self) -> int:
        return self.N_E + self.N_I

    @property
    def is_E(self) -> np.ndarray:
        return np.r_[np.ones(self.N_E, bool), np.zeros(self.N_I, bool)]


def spectral_radius(W: np.ndarray) -> float:
    return float(np.max(np.abs(np.linalg.eigvals(np.asarray(W, dtype=np.float64)))))


def _sample_sites(cfg: ReservoirConfig):
    c = cfg.connectivity
    seed = resolve_seeds(cfg.seeds)["topology"]
    rng = numpy_rng(seed)
    NE, NI = cfg.N_E, cfg.N_I
    N = NE + NI
    is_E = np.r_[np.ones(NE, bool), np.zeros(NI, bool)]

    coords = rng.random((N, 2))
    if c.use_fitness:
        fE = rng.pareto(c.pareto_alpha, NE) + 1.0
        fI = rng.pareto(c.pareto_alpha, NI) + 1.0 if NI else np.zeros(0)
        fitness = np.r_[fE / fE.mean(), fI / fI.mean() if NI else fI]
    else:
        fitness = np.ones(N)
    return seed, rng, is_E, coords, fitness


def _weight_block(c, rng, is_E, coords, fitness, start, stop):
    rows = slice(start, stop)
    diff = coords[rows, None, :] - coords[None, :, :]
    distance = np.sqrt((diff ** 2).sum(-1))           # [post, pre]

    if c.generator == "fitness_spatial":
        lam = np.where(is_E, c.lambda_E, c.lambda_I)  # indexed by presynaptic population
        same = is_E[rows, None] == is_E[None, :]
        f_post = np.where(same, fitness[rows, None], 1.0)
        p = c.p_scale * fitness[None, :] * f_post * np.exp(-distance / lam[None, :])
    elif c.generator == "uniform_random":
        p = np.full(distance.shape, c.uniform_density)
    else:
        raise ValueError(f"unknown generator {c.generator!r}")
    p[np.arange(stop - start), np.arange(start, stop)] = 0.0
    mask = rng.random(p.shape) < p
    W0 = mask.astype(float)
    W0[:, ~is_E] *= -c.inh_to_exc_magnitude_ratio
    W0[np.ix_(~is_E[rows], ~is_E)] *= c.ii_attenuation
    W0[~mask] = 0.0
    return W0, mask, distance


def build_dale_ei_structure(cfg: ReservoirConfig) -> DaleEIStructure:
    seed, rng, is_E, coords, fitness = _sample_sites(cfg)
    NE, NI = cfg.N_E, cfg.N_I
    W0, mask, distance = _weight_block(cfg.connectivity, rng, is_E, coords, fitness, 0, cfg.N)
    return DaleEIStructure(NE, NI, W0, mask, coords, fitness, distance, seed, spectral_radius(W0))


def sparse_global_weights(cfg: ReservoirConfig):
    """Same seeded graph, sampled in row blocks. Estimate rho on the sparse base."""
    from scipy.sparse import csr_matrix, vstack
    from scipy.sparse.linalg import eigs

    if cfg.scaling.mode != "global":
        raise ValueError("sparse_global_weights requires global scaling")
    _, rng, is_E, coords, fitness = _sample_sites(cfg)
    blocks = []
    for start in range(0, cfg.N, 64):
        block, _, _ = _weight_block(cfg.connectivity, rng, is_E, coords, fitness,
                                     start, min(start + 64, cfg.N))
        blocks.append(csr_matrix(block))
    base = vstack(blocks, format="csr")
    if cfg.N <= 8:
        rho = spectral_radius(base.toarray())
    elif base.nnz:
        values = eigs(base, k=4, which="LM", v0=np.random.default_rng(0).normal(size=cfg.N),
                      tol=1e-9, return_eigenvectors=False)
        rho = float(np.abs(values).max())
    else:
        rho = 0.0
    if rho <= 0:
        raise ValueError("base matrix has zero spectral radius")
    base.data *= cfg.scaling.g_global / rho
    return base


def dale_violations(W: np.ndarray, N_E: int) -> int:
    """Number of presynaptic columns whose nonzero signs contradict their population."""

    W = np.asarray(W)
    bad_E = np.any(W[:, :N_E] < 0, axis=0).sum()
    bad_I = np.any(W[:, N_E:] > 0, axis=0).sum()
    return int(bad_E + bad_I)
