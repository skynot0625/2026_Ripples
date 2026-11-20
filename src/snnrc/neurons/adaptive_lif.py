"""Discrete-time (adaptive) leaky integrate-and-fire neurons.

All state tensors are ``(batch, n)``.  One call to :meth:`AdaptiveLIF.step`
advances one ``dt``.  Corrected semantics (the default)::

    was_ref   = ref > 0 ;  ref[was_ref] -= 1
    I_eff     = current - w                        # w from the previous step
    v_int     = a_m * v + I_eff                    # "additive"
              | a_m * v + (1 - a_m) * I_eff        # "low_pass"
    v_int    += noise + kick                       # kick = voltage increment
    v         = v_reset where was_ref else v_int
    spike     = (v >= threshold) & ~was_ref
    v[spike]  = v_reset (hard) | v - threshold (subtract)
    ref[spike]= refractory_steps                   # blocks exactly k later steps
    w         = a_w * w + b * spike

``a_m = exp(-dt/tau_m)``, ``a_w = exp(-dt/tau_w)``.

Legacy options (only via explicitly named legacy configs):
``refractory_off_by_one`` stores ``k - 1`` (k-1 blocked steps, as in all legacy
criticality code) and ``kick_before_leak`` adds the kick before the decay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

import torch

Tensorish = Union[float, int, torch.Tensor]


def _vec(x: Tensorish, n: int, dtype, device) -> torch.Tensor:
    t = torch.as_tensor(x, dtype=dtype, device=device)
    if t.ndim == 0:
        t = t.expand(n).clone()
    if t.shape != (n,):
        raise ValueError(f"expected shape ({n},), got {tuple(t.shape)}")
    return t


@dataclass
class NeuronState:
    v: torch.Tensor
    w: torch.Tensor
    ref: torch.Tensor


class AdaptiveLIF:
    """Per-neuron parameterised adaptive LIF population (b = 0 -> plain LIF)."""

    def __init__(
        self,
        n: int,
        tau_m_ms: Tensorish,
        dt_ms: float,
        threshold: Tensorish = 1.0,
        v_reset: Tensorish = 0.0,
        reset_mode: str = "hard",
        refractory_steps: Tensorish = 0,
        tau_w_ms: Tensorish = 1.0,
        b: Tensorish = 0.0,
        current_integration: str = "additive",
        refractory_off_by_one: bool = False,
        kick_before_leak: bool = False,
        dtype: torch.dtype = torch.float64,
        device: str | torch.device = "cpu",
    ) -> None:
        if current_integration not in ("additive", "low_pass"):
            raise ValueError("current_integration must be 'additive' or 'low_pass'")
        self.n = int(n)
        self.dt_ms = float(dt_ms)
        self.dtype, self.device = dtype, torch.device(device)
        mk = lambda x: _vec(x, self.n, dtype, self.device)  # noqa: E731
        self.tau_m_ms = mk(tau_m_ms)
        self.threshold = mk(threshold)
        self.v_reset = mk(v_reset)
        if isinstance(reset_mode, str):
            if reset_mode not in ("hard", "subtract"):
                raise ValueError("reset_mode must be 'hard' or 'subtract'")
            self.subtract_reset = torch.full((self.n,), reset_mode == "subtract", device=self.device)
        else:
            self.subtract_reset = torch.as_tensor(reset_mode, dtype=torch.bool, device=self.device)
        self.refractory_steps = _vec(refractory_steps, self.n, torch.long, self.device)
        if (self.refractory_steps < 0).any():
            raise ValueError("refractory_steps must be >= 0")
        self.tau_w_ms = mk(tau_w_ms)
        self.b = mk(b)
        self.current_integration = current_integration
        self.refractory_off_by_one = bool(refractory_off_by_one)
        self.kick_before_leak = bool(kick_before_leak)
        self.alpha_m = torch.exp(-self.dt_ms / self.tau_m_ms)
        self.alpha_w = torch.exp(-self.dt_ms / self.tau_w_ms)
        stored = self.refractory_steps - 1 if self.refractory_off_by_one else self.refractory_steps
        self._ref_on_spike = stored.clamp(min=0)

    @property
    def adaptive(self) -> torch.Tensor:
        return self.b != 0

    def init_state(self, batch: int) -> NeuronState:
        z = torch.zeros(batch, self.n, dtype=self.dtype, device=self.device)
        return NeuronState(v=z + self.v_reset, w=z.clone(),
                           ref=torch.zeros(batch, self.n, dtype=torch.long, device=self.device))

    def reset_(self, state: NeuronState, rows: Optional[torch.Tensor] = None) -> None:
        if rows is None:
            state.v.copy_(self.v_reset.expand_as(state.v)); state.w.zero_(); state.ref.zero_()
        else:
            state.v[rows] = self.v_reset; state.w[rows] = 0; state.ref[rows] = 0

    @torch.no_grad()
    def step(
        self,
        state: NeuronState,
        current: torch.Tensor,
        noise: Optional[torch.Tensor] = None,
        kick: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        """Advance one step in place; return boolean spikes ``(batch, n)``."""

        was_ref = state.ref > 0
        state.ref.sub_(was_ref.long())
        i_eff = current - state.w
        v = state.v
        if kick is not None and self.kick_before_leak:
            v = v + kick
        if self.current_integration == "additive":
            v_int = self.alpha_m * v + i_eff
        else:
            v_int = self.alpha_m * v + (1.0 - self.alpha_m) * i_eff
        if noise is not None:
            v_int = v_int + noise
        if kick is not None and not self.kick_before_leak:
            v_int = v_int + kick
        v_new = torch.where(was_ref, self.v_reset.expand_as(v_int), v_int)
        spikes = (v_new >= self.threshold) & ~was_ref
        reset_val = torch.where(self.subtract_reset, v_new - self.threshold, self.v_reset.expand_as(v_new))
        v_new = torch.where(spikes, reset_val, v_new)
        state.v = v_new
        state.ref = torch.where(spikes, self._ref_on_spike.expand_as(state.ref), state.ref)
        state.w = self.alpha_w * state.w + self.b * spikes.to(self.dtype)
        return spikes


class LIF(AdaptiveLIF):
    """Non-adaptive LIF (adaptation disabled: b = 0)."""

    def __init__(self, n: int, tau_m_ms: Tensorish, dt_ms: float, **kw) -> None:
        kw.pop("b", None)
        kw.pop("tau_w_ms", None)
        super().__init__(n, tau_m_ms, dt_ms, b=0.0, tau_w_ms=1.0, **kw)
