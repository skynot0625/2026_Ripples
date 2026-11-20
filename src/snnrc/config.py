"""Typed configuration for the shared sRC core.

Every scientific parameter lives in a dataclass, is validated early, and is
serialised verbatim into each run directory.  Legacy (historically buggy)
semantics are only reachable through :class:`LegacySemantics` flags, which are
all ``False`` in every scientific preset; :meth:`ReservoirConfig.validate`
refuses to mix them silently (see ``semantics_label``).

Units
-----
* time constants, delays, durations: milliseconds
* ``dt_ms``: simulation step in milliseconds
* voltages and currents: dimensionless (threshold = 1.0 in the biological model)
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Tuple, Type, TypeVar

T = TypeVar("T")


# --------------------------------------------------------------------------- seeds
@dataclass
class SeedConfig:
    """Independent named seeds.  ``None`` fields are derived from ``master``.

    Changing one named seed never changes the random draws controlled by the
    others (each seed drives its own generator; see :mod:`snnrc.seeding`).
    """

    master: int = 0
    topology: Optional[int] = None       # coordinates, fitness, connection masks
    neuron_params: Optional[int] = None  # weak/strong assignment, tau_w draws
    delays: Optional[int] = None         # B1 conduction delays
    input_weights: Optional[int] = None  # W_in
    noise: Optional[int] = None          # membrane noise stream
    kick: Optional[int] = None           # kick target selection
    data: Optional[int] = None           # dataset splits / loaders (tasks)
    readout: Optional[int] = None        # readout initialisation (tasks)


# --------------------------------------------------------------------------- time conversion
def ms_to_steps(ms: float, dt_ms: float, what: str, exact: bool = True) -> int:
    """Convert a biologically defined interval (ms) to integer simulation steps.

    ``exact=True`` (refractory periods, closure windows, quiet / cooldown periods,
    burn-in, safety durations) requires ``ms`` to be an integer multiple of
    ``dt_ms`` (tolerance 1e-6 steps) so that intervals stay constant in ms when
    dt changes; anything else raises instead of silently rounding.
    """

    x = ms / dt_ms
    n = int(round(x))
    if exact and abs(x - n) > 1e-6:
        raise ValueError(f"{what}={ms} ms is not an integer multiple of dt={dt_ms} ms")
    if n < 0:
        raise ValueError(f"{what} must be non-negative")
    return n


# --------------------------------------------------------------------------- neurons
@dataclass
class NeuronPopulationConfig:
    tau_m_ms: float = 20.0
    threshold: float = 1.0
    v_reset: float = 0.0
    reset_mode: Literal["hard", "subtract"] = "hard"
    refractory_ms: float = 1.0          # converted with ms_to_steps(.., dt_ms)
    # "additive": v <- a v + I  (discrete form of the historical E/I models)
    # "low_pass": v <- a v + (1 - a) I  (low-pass form of the homogeneous reservoir)
    current_integration: Literal["additive", "low_pass"] = "additive"


@dataclass
class AdaptationGroupConfig:
    tau_w_mode_ms: float = 100.0
    tau_w_log_sigma: float = 0.3
    tau_w_clip_ms: Tuple[float, float] = (50.0, 1000.0)
    b: float = 0.05


@dataclass
class AdaptationConfig:
    """Excitatory spike-frequency adaptation (weak / strong subpopulations).

    ``tau_w`` is lognormal with the given **mode** (``mu = ln(mode) + sigma^2``)
    as in the legacy code, then clipped.
    """

    name: str = "historical_b020"
    weak_fraction: float = 0.6
    split_mode: Literal["exact", "bernoulli"] = "exact"
    weak: AdaptationGroupConfig = field(default_factory=AdaptationGroupConfig)
    strong: AdaptationGroupConfig = field(
        default_factory=lambda: AdaptationGroupConfig(300.0, 0.3, (100.0, 2000.0), 0.20)
    )
    inhibitory_b: float = 0.0
    inhibitory_tau_w_ms: float = 250.0


# --------------------------------------------------------------------------- connectivity
@dataclass
class ConnectivityConfig:
    """Fitness + spatial Dale E/I generator.

    ``p[post, pre] = p_scale * f_pre * (f_post if same population else 1)
    * exp(-d / lambda_pre_population)``; self-connections excluded.
    Base magnitudes (before scaling) are ``+1`` for E-presynaptic columns and
    ``-inh_to_exc_magnitude_ratio`` (I->E) or
    ``-inh_to_exc_magnitude_ratio * ii_attenuation`` (I->I) for I-presynaptic
    columns.
    """

    generator: Literal["fitness_spatial", "uniform_random"] = "fitness_spatial"
    lambda_E: float = 0.25
    lambda_I: float = 0.20
    pareto_alpha: float = 3.0
    p_scale: float = 0.2
    use_fitness: bool = True
    inh_to_exc_magnitude_ratio: float = 4.0   # |I->E| / |E->*| base magnitude (historically "J_I_ratio")
    ii_attenuation: float = 0.75
    uniform_density: float = 0.04             # only for generator="uniform_random"


@dataclass
class ScalingConfig:
    """Recurrent-strength manipulation.  Two mathematically different modes.

    ``mode="global"`` (historical parameterisation):
        ``W = g_global * W0 / rho(W0)`` -> ``rho(W) == g_global``; every block
        (E and I) scales together and the I/E magnitude ratio is fixed.
        Historical code called this global gain ``J_E_exc`` / ``J_E``; it is NOT
        the ``J_E_exc`` of ``mode="excitatory"``.

    ``mode="excitatory"`` (true excitatory manipulation):
        ``W[:, E] = J_E_exc * W0[:, E] / rho_ref`` and
        ``W[:, I] = (J_I / inh_to_exc_magnitude_ratio) * W0[:, I] / rho_ref``
        with ``rho_ref = rho(W0)``.  ``J_E_exc`` scales only E-presynaptic
        columns; ``J_I`` sets the I-presynaptic magnitude independently.
        Identity bridge: ``J_E_exc = g`` and ``J_I = ratio * g`` reproduce
        ``global`` with ``g_global = g`` exactly -- the only point at which the
        two parameterisations coincide.
    """

    mode: Literal["global", "excitatory"] = "global"
    g_global: float = 1.0
    J_E_exc: float = 1.0
    J_I: float = 4.0


@dataclass
class DelayConfig:
    """B0: ``mode="none"`` (one-step).  B1: per-presynaptic-neuron delays."""

    mode: Literal["none", "per_presynaptic"] = "none"
    mean_ms: float = 2.0
    log_sigma: float = 0.5
    clip_ms: Tuple[float, float] = (0.5, 8.0)
    rounding: Literal["round", "floor"] = "round"
    min_delay_steps: int = 1


@dataclass
class NoiseConfig:
    sigma: float = 0.05
    # "per_step": sigma is the per-update std (legacy semantics)
    # "sqrt_dt": per-update std = sigma * sqrt(dt_ms)
    scaling: Literal["per_step", "sqrt_dt"] = "per_step"
    # "shared_stream": one generator for the whole run (avalanche analysis)
    # "per_sample":    one generator per (noise seed, sample key) (tasks)
    mode: Literal["shared_stream", "per_sample"] = "shared_stream"


@dataclass
class InputConfig:
    n_inputs: int = 0
    target: Literal["E", "all"] = "E"
    weight_low: float = -1.5
    weight_high: float = 1.5


@dataclass
class LegacySemantics:
    """Historical behaviours.  All ``False`` in scientific configurations."""

    double_negative_inhibition: bool = False   # sign applied twice: inhibition acts as excitation
    ee_transposed: bool = False                # all legacy E/I code: E->E applied as W_EE^T
    refractory_off_by_one: bool = False        # ref_steps k -> k-1 blocked steps
    zero_delay_full_ring: bool = False         # floor() delays, delay 0 arrives after full ring
    kick_before_leak: bool = False             # SOC: v += kick before decay

    def any(self) -> bool:
        return any(dataclasses.asdict(self).values())


@dataclass
class ReservoirConfig:
    family: Literal["B0", "B1"] = "B0"
    name: str = "nprc_b0"
    N_E: int = 800
    N_I: int = 200
    dt_ms: float = 1.0
    dtype: Literal["float32", "float64"] = "float64"
    excitatory: NeuronPopulationConfig = field(default_factory=NeuronPopulationConfig)
    inhibitory: NeuronPopulationConfig = field(
        default_factory=lambda: NeuronPopulationConfig(tau_m_ms=5.0)
    )
    adaptation: AdaptationConfig = field(default_factory=AdaptationConfig)
    connectivity: ConnectivityConfig = field(default_factory=ConnectivityConfig)
    scaling: ScalingConfig = field(default_factory=ScalingConfig)
    delay: DelayConfig = field(default_factory=DelayConfig)
    noise: NoiseConfig = field(default_factory=NoiseConfig)
    input: InputConfig = field(default_factory=InputConfig)
    legacy: LegacySemantics = field(default_factory=LegacySemantics)
    seeds: SeedConfig = field(default_factory=SeedConfig)

    @property
    def N(self) -> int:
        return self.N_E + self.N_I

    @property
    def semantics_label(self) -> str:
        return "legacy" if self.legacy.any() else "corrected"

    def validate(self) -> "ReservoirConfig":
        if self.N_E <= 0 or self.N_I < 0:
            raise ValueError("N_E must be positive and N_I non-negative")
        if self.dt_ms <= 0:
            raise ValueError("dt_ms must be positive")
        if self.family == "B0" and self.delay.mode != "none":
            raise ValueError("B0 has no conduction delay; use family='B1'")
        if self.family == "B1" and self.delay.mode == "none":
            raise ValueError("B1 requires delay.mode='per_presynaptic'")
        if self.delay.min_delay_steps < 1 and not self.legacy.zero_delay_full_ring:
            raise ValueError("corrected delay semantics require min_delay_steps >= 1")
        for pop in (self.excitatory, self.inhibitory):
            ms_to_steps(pop.refractory_ms, self.dt_ms, "refractory_ms")
            if pop.tau_m_ms <= 0:
                raise ValueError("tau_m_ms must be positive")
        if not 0.0 <= self.adaptation.weak_fraction <= 1.0:
            raise ValueError("weak_fraction must be in [0, 1]")
        if self.legacy.any() and not self.name.startswith("legacy_"):
            raise ValueError(
                "legacy semantics may only be enabled in configs whose name starts "
                f"with 'legacy_' (got name={self.name!r})"
            )
        if self.name.startswith("legacy_") and not self.legacy.any():
            raise ValueError("a 'legacy_' config must enable at least one legacy flag")
        return self


# --------------------------------------------------------------------------- avalanche
@dataclass
class KickConfig:
    amplitude: float = 1.5
    target_population: Literal["E", "I", "all"] = "E"
    application: Literal["voltage_increment"] = "voltage_increment"
    quiet_population: Literal["E", "all"] = "E"
    quiet_before_kick_ms: float = 100.0
    cooldown_ms: float = 100.0
    require_no_active_avalanche: bool = True
    require_no_pending_spikes: bool = True
    # what to do if the chosen target is refractory
    on_refractory_target: Literal["log_failed", "retarget"] = "log_failed"


@dataclass
class SegmentationConfig:
    """One avalanche definition.  Replayed offline from saved per-step counts."""

    name: str = "primary_E_seg_E_size"
    boundary_population: Literal["E", "total"] = "E"
    size_population: Literal["E", "total"] = "E"
    close_silence_ms: float = 5.0       # converted with ms_to_steps(.., dt_ms)
    require_no_pending: bool = True


@dataclass
class SafetyConfig:
    max_size_spikes: int = 16000          # 20 * N_E
    max_duration_ms: float = 20000.0
    action: Literal["reset_state", "mark_only", "terminate_run"] = "reset_state"
    # terminate_run: stop the simulation at the first crossing (no state reset);
    # data up to and including the crossing step are kept; run_status = runaway_terminated


@dataclass
class AvalancheRunConfig:
    reservoir: ReservoirConfig = field(default_factory=ReservoirConfig)
    duration_ms: float = 10000.0
    burn_in_ms: float = 1000.0
    kick: KickConfig = field(default_factory=KickConfig)
    primary_segmentation: SegmentationConfig = field(default_factory=SegmentationConfig)
    secondary_segmentations: Tuple[SegmentationConfig, ...] = (
        SegmentationConfig("A_total_seg_E_size", "total", "E", 5.0, True),
        SegmentationConfig("B_total_seg_total_size", "total", "total", 5.0, True),
    )
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    protocol_label: str = "reconstructed_nprc_kick_protocol"
    device: str = "cpu"

    def n_steps(self) -> int:
        n = self.duration_ms / self.reservoir.dt_ms
        if abs(n - round(n)) > 1e-9:
            raise ValueError("duration_ms must be an integer multiple of dt_ms")
        return int(round(n))

    def steps(self, ms: float, what: str = "interval") -> int:
        return ms_to_steps(ms, self.reservoir.dt_ms, what)

    def validate(self) -> "AvalancheRunConfig":
        self.reservoir.validate()
        self.n_steps()
        for what, ms in (("burn_in_ms", self.burn_in_ms), ("quiet_before_kick_ms", self.kick.quiet_before_kick_ms),
                         ("cooldown_ms", self.kick.cooldown_ms), ("safety.max_duration_ms", self.safety.max_duration_ms)):
            self.steps(ms, what)
        for sd in (self.primary_segmentation, *self.secondary_segmentations):
            if self.steps(sd.close_silence_ms, f"{sd.name}.close_silence_ms") < 1:
                raise ValueError("close_silence_ms must be at least one step")
        return self

    def step_table(self) -> dict:
        """Every ms-defined interval with its step count at this dt (saved in manifests)."""

        dt = self.reservoir.dt_ms
        r = self.reservoir
        out = {"dt_ms": dt, "n_steps": self.n_steps(),
               "refractory_E_steps": ms_to_steps(r.excitatory.refractory_ms, dt, "E refractory"),
               "refractory_I_steps": ms_to_steps(r.inhibitory.refractory_ms, dt, "I refractory"),
               "burn_in_steps": self.steps(self.burn_in_ms),
               "quiet_before_kick_steps": self.steps(self.kick.quiet_before_kick_ms),
               "cooldown_steps": self.steps(self.kick.cooldown_ms),
               "safety_max_duration_steps": self.steps(self.safety.max_duration_ms)}
        for sd in (self.primary_segmentation, *self.secondary_segmentations):
            out[f"close_silence_steps[{sd.name}]"] = self.steps(sd.close_silence_ms)
        return out


# --------------------------------------------------------------------------- (de)serialisation
def to_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
    """Recursively build dataclass ``cls`` from a plain dict (unknown keys fail)."""

    if not dataclasses.is_dataclass(cls):
        return data  # type: ignore[return-value]
    hints = _resolved_hints(cls)
    kwargs = {}
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise KeyError(f"Unknown config keys for {cls.__name__}: {sorted(unknown)}")
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        ftype = hints[f.name]
        if dataclasses.is_dataclass(ftype) and isinstance(value, dict):
            value = from_dict(ftype, value)
        elif f.name == "secondary_segmentations":
            value = tuple(from_dict(SegmentationConfig, v) for v in value)
        elif isinstance(value, list):
            value = tuple(value)
        kwargs[f.name] = value
    return cls(**kwargs)


def _resolved_hints(cls: type) -> Dict[str, Any]:
    import typing

    return typing.get_type_hints(cls)


def save_config(obj: Any, path: Path) -> None:
    path = Path(path)
    data = to_dict(obj)
    if path.suffix in {".yaml", ".yml"}:
        import yaml

        path.write_text(yaml.safe_dump(data, sort_keys=False))
    else:
        path.write_text(json.dumps(data, indent=2))


def load_config(cls: Type[T], path: Path) -> T:
    path = Path(path)
    if path.suffix in {".yaml", ".yml"}:
        import yaml

        data = yaml.safe_load(path.read_text())
    else:
        data = json.loads(path.read_text())
    return from_dict(cls, data)
