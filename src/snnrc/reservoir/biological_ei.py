"""B0 / B1 biological E/I reservoir (shared by the avalanche analysis and downstream tasks).

Single step at time index ``t`` (state tensors ``(batch, N)``)::

    arriving   = ring[t mod D];  ring[t mod D] = 0
    I_syn      = arriving @ W_applied.T               # = W[post, pre] @ s  per sample
    I          = I_syn + u_t @ W_in.T (+ ext_current)
    s_t        = neurons.step(I, noise_t, kick_t)     # see neurons.adaptive_lif
    ring[(t + d_pre) mod D, pre] += s_t[pre]          # for every spiking presynaptic neuron

with ``D = max_delay + 1`` ring slots.

* **B0**: ``d_pre = 1`` for all neurons (one-step recurrence: a spike at t
  contributes current at t+1).  ``D = 2``.
* **B1**: ``d_pre`` in ``[min_delay_steps >= 1, max_delay]`` per presynaptic
  neuron.  Because ``1 <= d <= D-1`` a write never targets the slot that was
  just read, so a spike emitted at t arrives at exactly ``t + d``.

``pending_spikes`` = number of spikes in the ring after the step (spikes
emitted but not yet delivered).  For B0 this equals the spikes emitted at t.

Legacy flags (``config.legacy``, only in configs named ``legacy_*``):
``ee_transposed`` (E->E applied as ``W_EE^T``), ``double_negative_inhibition``
(I-presynaptic columns sign-flipped -> excitatory), ``refractory_off_by_one``,
``zero_delay_full_ring`` (floor rounding, delay 0 allowed and delivered after
``D`` steps -- exactly the legacy ring behaviour), ``kick_before_leak``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import torch

from ..config import ReservoirConfig, ms_to_steps
from ..connectivity import apply_scaling, build_dale_ei_structure
from ..connectivity.dale_ei import DaleEIStructure, sparse_global_weights
from ..neurons import AdaptiveLIF, NeuronState
from ..seeding import numpy_rng, per_sample_seed, resolve_seeds, torch_generator

DTYPES = {"float32": torch.float32, "float64": torch.float64}


# --------------------------------------------------------------------------- sampling
def sample_adaptation(cfg: ReservoirConfig, seed: int) -> Dict[str, np.ndarray]:
    a = cfg.adaptation
    rng = numpy_rng(seed)
    NE, NI = cfg.N_E, cfg.N_I
    if a.split_mode == "exact":
        n_weak = int(round(a.weak_fraction * NE))
        is_weak = np.zeros(NE, bool)
        is_weak[rng.permutation(NE)[:n_weak]] = True
    else:
        is_weak = rng.random(NE) < a.weak_fraction

    def lognormal_mode(group, n):
        mu = np.log(group.tau_w_mode_ms) + group.tau_w_log_sigma ** 2
        return np.clip(rng.lognormal(mu, group.tau_w_log_sigma, n), *group.tau_w_clip_ms)

    tau_w = np.empty(NE + NI)
    b = np.empty(NE + NI)
    tau_w[:NE][is_weak] = lognormal_mode(a.weak, int(is_weak.sum()))
    tau_w[:NE][~is_weak] = lognormal_mode(a.strong, int((~is_weak).sum()))
    b[:NE] = np.where(is_weak, a.weak.b, a.strong.b)
    tau_w[NE:] = a.inhibitory_tau_w_ms
    b[NE:] = a.inhibitory_b
    return {"is_weak": is_weak, "tau_w": tau_w, "b": b}


def sample_delay_ms(cfg: ReservoirConfig, seed: int) -> np.ndarray:
    """Per-presynaptic conduction delays in ms (independent of dt; same seed -> same ms)."""

    d = cfg.delay
    rng = numpy_rng(seed)
    mu = np.log(d.mean_ms) - d.log_sigma ** 2 / 2.0        # mean parameterisation (legacy)
    return np.clip(rng.lognormal(mu, d.log_sigma, cfg.N), *d.clip_ms)


def sample_delays(cfg: ReservoirConfig, seed: int) -> np.ndarray:
    """Delays in steps: ``round(ms / dt)`` (or floor in legacy configs), clamped to ``min_delay_steps``."""

    d = cfg.delay
    if d.mode == "none":
        return np.ones(cfg.N, dtype=np.int64)
    steps = sample_delay_ms(cfg, seed) / cfg.dt_ms
    steps = np.floor(steps + 1e-9) if d.rounding == "floor" else np.round(steps)
    return np.maximum(steps.astype(np.int64), d.min_delay_steps)


def sample_input_weights(cfg: ReservoirConfig, seed: int) -> np.ndarray:
    inp = cfg.input
    W_in = np.zeros((cfg.N, inp.n_inputs))
    if inp.n_inputs == 0:
        return W_in
    rng = numpy_rng(seed)
    rows = cfg.N_E if inp.target == "E" else cfg.N
    W_in[:rows] = rng.uniform(inp.weight_low, inp.weight_high, size=(rows, inp.n_inputs))
    return W_in


# --------------------------------------------------------------------------- state
@dataclass
class ReservoirState:
    neurons: NeuronState
    buffer: torch.Tensor                       # (D, batch, N) spike counts in flight
    t: int = 0
    noise_generators: Optional[List[torch.Generator]] = None
    last_synaptic_current: Optional[torch.Tensor] = None
    sample_keys: Optional[Sequence[int]] = field(default=None)


class BiologicalEIReservoir:
    def __init__(self, cfg: ReservoirConfig, device: str | torch.device = "cpu",
                 structure: Optional[DaleEIStructure] = None, *, blockwise_global: bool = False) -> None:
        cfg.validate()
        self.cfg = cfg
        self.seeds = resolve_seeds(cfg.seeds)
        if blockwise_global:
            if structure is not None:
                raise ValueError("blockwise_global builds its own seeded structure")
            self.structure = None
            # Presynaptic columns are contiguous for the single-sample LED stream.
            W = sparse_global_weights(cfg).toarray(order="F")
        else:
            self.structure = structure if structure is not None else build_dale_ei_structure(cfg)
            W = apply_scaling(self.structure, cfg.scaling, cfg.connectivity.inh_to_exc_magnitude_ratio)
        adapt = sample_adaptation(cfg, self.seeds["neuron_params"])
        self.is_weak_E = adapt["is_weak"]
        tau_m = np.r_[np.full(cfg.N_E, cfg.excitatory.tau_m_ms), np.full(cfg.N_I, cfg.inhibitory.tau_m_ms)]
        delays = sample_delays(cfg, self.seeds["delays"])
        W_in = sample_input_weights(cfg, self.seeds["input_weights"])
        self._setup(W, W_in, tau_m, adapt["tau_w"], adapt["b"], delays, None, device)

    @classmethod
    def from_arrays(cls, cfg: ReservoirConfig, W, W_in, tau_m, tau_w, b, delay_steps,
                    max_delay_steps: Optional[int] = None, device="cpu") -> "BiologicalEIReservoir":
        """Build from explicit arrays (``W`` as stored ``[post, pre]``), e.g. legacy instances."""

        cfg.validate()
        self = cls.__new__(cls)
        self.cfg = cfg
        self.seeds = resolve_seeds(cfg.seeds)
        self.structure = None
        self.is_weak_E = None
        self._setup(np.asarray(W), np.asarray(W_in), np.asarray(tau_m), np.asarray(tau_w),
                    np.asarray(b), np.asarray(delay_steps), max_delay_steps, device)
        return self

    # ------------------------------------------------------------------ setup
    def _setup(self, W, W_in, tau_m, tau_w, b, delays, max_delay, device) -> None:
        cfg = self.cfg
        self.device = torch.device(device)
        self.dtype = DTYPES[cfg.dtype]
        self.N_E, self.N_I, self.N = cfg.N_E, cfg.N_I, cfg.N
        self.is_E = torch.zeros(self.N, dtype=torch.bool, device=self.device)
        self.is_E[: self.N_E] = True
        if cfg.excitatory.current_integration != cfg.inhibitory.current_integration:
            raise ValueError("E and I must share current_integration")
        pops = [(cfg.excitatory, self.N_E), (cfg.inhibitory, self.N_I)]
        rep = lambda attr: np.concatenate([np.full(n, getattr(p, attr)) for p, n in pops])  # noqa: E731
        self.neurons = AdaptiveLIF(
            self.N, tau_m_ms=torch.as_tensor(tau_m, dtype=self.dtype), dt_ms=cfg.dt_ms,
            threshold=rep("threshold"), v_reset=rep("v_reset"),
            reset_mode=torch.as_tensor(rep("reset_mode") == "subtract"),
            refractory_steps=torch.as_tensor(np.concatenate([
                np.full(n, ms_to_steps(p.refractory_ms, cfg.dt_ms, "refractory_ms")) for p, n in pops]).astype(np.int64)),
            tau_w_ms=torch.as_tensor(tau_w, dtype=self.dtype), b=torch.as_tensor(b, dtype=self.dtype),
            current_integration=cfg.excitatory.current_integration,
            refractory_off_by_one=cfg.legacy.refractory_off_by_one,
            kick_before_leak=cfg.legacy.kick_before_leak,
            dtype=self.dtype, device=self.device,
        )
        self.W_in = torch.as_tensor(W_in, dtype=self.dtype, device=self.device)
        self.set_recurrent_weights(torch.as_tensor(W, dtype=self.dtype))
        self.set_delays(torch.as_tensor(delays, dtype=torch.long), max_delay)
        sig = cfg.noise.sigma * (np.sqrt(cfg.dt_ms) if cfg.noise.scaling == "sqrt_dt" else 1.0)
        self.noise_std = float(sig)

    def set_recurrent_weights(self, W: torch.Tensor) -> None:
        """Set the stored ``W[post, pre]`` and derive the applied operator."""

        W = torch.as_tensor(W, dtype=self.dtype).to(self.device).clone()
        NE = self.N_E
        Wa = W.clone()
        if self.cfg.legacy.ee_transposed:
            Wa[:NE, :NE] = W[:NE, :NE].T
        if self.cfg.legacy.double_negative_inhibition:
            Wa[:, NE:] = -W[:, NE:]
        self.W = W
        self.W_applied = Wa

    def set_delays(self, delays: torch.Tensor, max_delay_steps: Optional[int] = None) -> None:
        delays = torch.as_tensor(delays, dtype=torch.long).to(self.device).clone()
        if not self.cfg.legacy.zero_delay_full_ring and int(delays.min()) < 1:
            raise ValueError("corrected delay semantics require delays >= 1 step")
        md = int(delays.max()) if max_delay_steps is None else int(max_delay_steps)
        if md < int(delays.max()):
            raise ValueError("max_delay_steps smaller than largest delay")
        self.delay_steps = delays
        self.max_delay_steps = md
        self.ring_size = md + 1

    def tensors(self) -> Dict[str, torch.Tensor]:
        return {"W": self.W, "W_applied": self.W_applied, "W_in": self.W_in,
                "tau_m": self.neurons.tau_m_ms, "tau_w": self.neurons.tau_w_ms, "b": self.neurons.b,
                "alpha_m": self.neurons.alpha_m, "alpha_w": self.neurons.alpha_w,
                "threshold": self.neurons.threshold, "delay_steps": self.delay_steps}

    # ------------------------------------------------------------------ dynamics
    def reset_state(self, batch: int = 1, sample_keys: Optional[Sequence[int]] = None) -> ReservoirState:
        gens = None
        if self.noise_std > 0:
            if self.cfg.noise.mode == "per_sample":
                if sample_keys is None or len(sample_keys) != batch:
                    raise ValueError("per_sample noise requires one sample key per batch row")
                gens = [torch_generator(per_sample_seed(self.seeds["noise"], k), self.device) for k in sample_keys]
            else:
                gens = [torch_generator(self.seeds["noise"], self.device)]
        return ReservoirState(
            neurons=self.neurons.init_state(batch),
            buffer=torch.zeros(self.ring_size, batch, self.N, dtype=self.dtype, device=self.device),
            noise_generators=gens, sample_keys=sample_keys,
        )

    def reset_dynamics_(self, st: ReservoirState) -> None:
        """Safety reset: v = v_reset, w = 0, refractory = 0, ring cleared (noise stream continues)."""

        self.neurons.reset_(st.neurons)
        st.buffer.zero_()

    def _noise(self, st: ReservoirState, batch: int) -> Optional[torch.Tensor]:
        if not st.noise_generators:
            return None
        if self.cfg.noise.mode == "per_sample":
            rows = [torch.randn(self.N, generator=g, dtype=self.dtype, device=self.device)
                    for g in st.noise_generators]
            return torch.stack(rows) * self.noise_std
        return torch.randn(batch, self.N, generator=st.noise_generators[0],
                           dtype=self.dtype, device=self.device) * self.noise_std

    @torch.no_grad()
    def step(self, st: ReservoirState, u: Optional[torch.Tensor] = None,
             ext_current: Optional[torch.Tensor] = None, kick: Optional[torch.Tensor] = None) -> torch.Tensor:
        batch = st.buffer.shape[1]
        slot = st.t % self.ring_size
        arriving = st.buffer[slot].clone()
        st.buffer[slot].zero_()
        if batch == 1:
            idx = torch.nonzero(arriving[0]).flatten()
            if idx.numel():
                i_syn = (self.W_applied[:, idx] @ arriving[0, idx]).unsqueeze(0)
            else:
                i_syn = torch.zeros(1, self.N, dtype=self.dtype, device=self.device)
        else:
            i_syn = arriving @ self.W_applied.T
        current = i_syn
        if u is not None:
            current = current + torch.as_tensor(u, dtype=self.dtype, device=self.device) @ self.W_in.T
        if ext_current is not None:
            current = current + ext_current
        spikes = self.neurons.step(st.neurons, current, self._noise(st, batch), kick)
        if spikes.any():
            b_idx, n_idx = torch.nonzero(spikes, as_tuple=True)
            target = (st.t + self.delay_steps[n_idx]) % self.ring_size
            st.buffer.index_put_((target, b_idx, n_idx),
                                 torch.ones(n_idx.numel(), dtype=self.dtype, device=self.device),
                                 accumulate=True)
        st.last_synaptic_current = i_syn
        st.t += 1
        return spikes

    def pending_spikes(self, st: ReservoirState) -> torch.Tensor:
        return st.buffer.sum(dim=(0, 2))

    @torch.no_grad()
    def run(self, u: Optional[torch.Tensor] = None, n_steps: Optional[int] = None, batch: int = 1,
            sample_keys: Optional[Sequence[int]] = None, record_voltage: bool = False) -> Dict[str, torch.Tensor]:
        """Run from a fresh state.  ``u``: ``(batch, T, n_inputs)``."""

        if u is not None:
            u = torch.as_tensor(u, dtype=self.dtype, device=self.device)
            batch, n_steps = u.shape[0], u.shape[1]
        st = self.reset_state(batch, sample_keys)
        spikes, volts = [], []
        for t in range(int(n_steps)):
            spikes.append(self.step(st, None if u is None else u[:, t]))
            if record_voltage:
                volts.append(st.neurons.v.clone())
        out = {"spikes": torch.stack(spikes, dim=1)}
        if record_voltage:
            out["voltage"] = torch.stack(volts, dim=1)
        return out

    # ------------------------------------------------------------------ provenance
    def fingerprint(self, compute_rho: bool = True) -> dict:
        from ..provenance import reservoir_fingerprint

        return reservoir_fingerprint(self, compute_rho=compute_rho)
