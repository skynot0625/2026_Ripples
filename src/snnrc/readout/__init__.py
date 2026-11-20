"""Readouts.  Only readout parameters are ever trained in the primary RC protocol."""

from __future__ import annotations

from typing import Optional

import numpy as np
import torch
from scipy import linalg


class RidgeReadout:
    """Multi-output ridge regression ``Y ~ X W^T``.

    ``penalize_bias=True`` regularises the bias like every other weight (ridge
    added to the whole Gram diagonal, primal if p <= n else dual).  ``penalize_bias=False``
    leaves the (last, constant) bias column unregularised (primal solve).
    """

    def __init__(self, alpha: float, has_bias: bool = True, penalize_bias: bool = False) -> None:
        if alpha <= 0:
            raise ValueError("alpha must be positive")
        self.alpha, self.has_bias, self.penalize_bias = float(alpha), bool(has_bias), bool(penalize_bias)
        self.weights: Optional[np.ndarray] = None

    def fit(self, X: np.ndarray, Y: np.ndarray) -> "RidgeReadout":
        X = np.asarray(X, float); Y = np.asarray(Y, float)
        if X.ndim != 2 or Y.ndim != 2 or X.shape[0] != Y.shape[0]:
            raise ValueError(f"bad shapes X={X.shape} Y={Y.shape}")
        if self.has_bias and not np.allclose(X[:, -1], X[0, -1]):
            raise ValueError("has_bias=True requires a constant last column")
        n, p = X.shape
        if self.penalize_bias or not self.has_bias:
            if p <= n:
                G = X.T @ X
                G.flat[:: p + 1] += self.alpha
                self.weights = linalg.solve(G, X.T @ Y, assume_a="positive definite", check_finite=False).T
            else:
                G = X @ X.T
                G.flat[:: n + 1] += self.alpha
                dual = linalg.solve(G, Y, assume_a="positive definite", check_finite=False)
                self.weights = (X.T @ dual).T
        else:
            G = X.T @ X
            P = np.full(p, self.alpha); P[-1] = 0.0
            G.flat[:: p + 1] += P
            self.weights = linalg.solve(G, X.T @ Y, assume_a="symmetric", check_finite=False).T
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.weights is None:
            raise RuntimeError("not fitted")
        return np.asarray(X, float) @ self.weights.T


class LinearClassificationReadout(torch.nn.Module):
    """``logits = sum_t mask_t * Linear(f_t)`` (equals Linear of accumulated features up to bias)."""

    def __init__(self, n_features: int, n_classes: int, bias: bool = False, dtype=torch.float32) -> None:
        super().__init__()
        self.linear = torch.nn.Linear(n_features, n_classes, bias=bias, dtype=dtype)

    def forward(self, features: torch.Tensor, mask: Optional[torch.Tensor] = None) -> torch.Tensor:
        if mask is None:
            return self.linear(features.sum(dim=1)) if self.linear.bias is None else self.linear(features).sum(1)
        return (self.linear(features) * mask[..., None]).sum(dim=1)


def assert_reservoir_frozen(reservoir) -> None:
    for name, t in reservoir.tensors().items():
        if isinstance(t, torch.Tensor) and t.requires_grad:
            raise RuntimeError(f"reservoir tensor {name} requires grad; primary RC protocol forbids it")


def readout_optimizer(readout: torch.nn.Module, reservoir, lr: float = 1e-3, weight_decay: float = 0.0):
    """Adam over readout parameters only, after verifying the reservoir is frozen."""

    assert_reservoir_frozen(reservoir)
    return torch.optim.Adam(readout.parameters(), lr=lr, weight_decay=weight_decay)


__all__ = ["RidgeReadout", "LinearClassificationReadout", "readout_optimizer", "assert_reservoir_frozen"]
