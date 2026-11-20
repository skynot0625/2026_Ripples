"""Named configurations.

Scientific presets (corrected semantics):
    ``nprc_b0``  -- biological E/I reservoir, no delay (B0)
    ``nprc_b1``  -- same with per-presynaptic conduction delay (B1)

Adaptation presets:
    ``historical_b020`` -- weak tau_w mode 100 ms, b=0.05; strong mode 300 ms, b=0.20
                           (the baseline)
    ``historical_b015`` -- identical except strong b=0.15 (ablation)

Legacy compatibility presets (NOT scientific defaults; names start with
``legacy_``): ``legacy_ei_ssc``, ``legacy_ei_0217``, ``legacy_ei_0215`` enable
the historical semantics flags.  They do not reproduce the historical
*topologies* (different builder and global ``np.random`` streams); that
requires the original historical code, which is not part of this package.
"""

from __future__ import annotations

import copy
from typing import Callable, Dict

from .config import (
    AdaptationConfig,
    AdaptationGroupConfig,
    AvalancheRunConfig,
    DelayConfig,
    LegacySemantics,
    NeuronPopulationConfig,
    ReservoirConfig,
    ScalingConfig,
    SeedConfig,
)


def historical_b020() -> AdaptationConfig:
    return AdaptationConfig(
        name="historical_b020",
        weak_fraction=0.6,
        split_mode="exact",
        weak=AdaptationGroupConfig(100.0, 0.3, (50.0, 1000.0), 0.05),
        strong=AdaptationGroupConfig(300.0, 0.3, (100.0, 2000.0), 0.20),
    )


def historical_b015() -> AdaptationConfig:
    a = historical_b020()
    a.name = "historical_b015"
    a.strong.b = 0.15
    return a


ADAPTATION_PRESETS: Dict[str, Callable[[], AdaptationConfig]] = {
    "historical_b020": historical_b020,
    "historical_b015": historical_b015,
}


def nprc_b0(
    g_global: float = 1.0,
    adaptation: str = "historical_b020",
    master_seed: int = 0,
) -> ReservoirConfig:
    return ReservoirConfig(
        family="B0",
        name=f"nprc_b0_{adaptation}",
        N_E=800,
        N_I=200,
        dt_ms=1.0,
        excitatory=NeuronPopulationConfig(tau_m_ms=20.0, refractory_ms=1.0),
        inhibitory=NeuronPopulationConfig(tau_m_ms=5.0, refractory_ms=1.0),
        adaptation=ADAPTATION_PRESETS[adaptation](),
        scaling=ScalingConfig(mode="global", g_global=g_global),
        delay=DelayConfig(mode="none"),
        seeds=SeedConfig(master=master_seed),
    ).validate()


def nprc_b1(
    g_global: float = 1.0,
    adaptation: str = "historical_b020",
    master_seed: int = 0,
) -> ReservoirConfig:
    cfg = nprc_b0(g_global, adaptation, master_seed)
    cfg.family = "B1"
    cfg.name = f"nprc_b1_{adaptation}"
    cfg.delay = DelayConfig(
        mode="per_presynaptic",
        mean_ms=2.0,
        log_sigma=0.5,
        clip_ms=(0.5, 8.0),
        rounding="round",
        min_delay_steps=1,
    )
    return cfg.validate()


def nprc_excitatory_sweep_point(
    J_E_exc: float, J_I: float, family: str = "B0", adaptation: str = "historical_b020",
    master_seed: int = 0,
) -> ReservoirConfig:
    """Separate scientific experiment: true excitatory scaling (NOT g_global)."""

    cfg = nprc_b0(1.0, adaptation, master_seed) if family == "B0" else nprc_b1(1.0, adaptation, master_seed)
    cfg.scaling = ScalingConfig(mode="excitatory", J_E_exc=J_E_exc, J_I=J_I)
    cfg.name = cfg.name.replace("nprc_", "nprc_excscale_")
    return cfg.validate()


# --------------------------------------------------------------------------- legacy
def _legacy_base(name: str, adaptation: AdaptationConfig, g: float) -> ReservoirConfig:
    return ReservoirConfig(
        family="B1",
        name=name,
        dt_ms=1.0,
        dtype="float32",
        excitatory=NeuronPopulationConfig(tau_m_ms=20.0, refractory_ms=1.0),
        inhibitory=NeuronPopulationConfig(tau_m_ms=5.0, refractory_ms=1.0),
        adaptation=adaptation,
        scaling=ScalingConfig(mode="global", g_global=g),
        delay=DelayConfig(
            mode="per_presynaptic", mean_ms=2.0, log_sigma=0.5, clip_ms=(0.5, 8.0),
            rounding="floor", min_delay_steps=0,
        ),
        legacy=LegacySemantics(
            double_negative_inhibition=False,
            ee_transposed=True,
            refractory_off_by_one=True,
            zero_delay_full_ring=True,
        ),
        seeds=SeedConfig(master=42),
    )


def legacy_ei_ssc(g: float = 5.0) -> ReservoirConfig:
    a = historical_b015()
    a.name = "legacy_ssc_b015"
    a.split_mode = "bernoulli"
    return _legacy_base("legacy_ei_ssc", a, g).validate()


def legacy_ei_0217(g: float = 4.5) -> ReservoirConfig:
    cfg = legacy_ei_ssc(g)
    cfg.name = "legacy_ei_0217"
    cfg.legacy.double_negative_inhibition = True
    cfg.connectivity.lambda_I = 0.10
    return cfg.validate()


def legacy_ei_0215(g: float = 1.0) -> ReservoirConfig:
    cfg = legacy_ei_0217(g)
    cfg.name = "legacy_ei_0215"
    cfg.adaptation = AdaptationConfig(
        name="legacy_0215",
        weak_fraction=0.6,
        split_mode="bernoulli",
        weak=AdaptationGroupConfig(250.0, 0.4, (50.0, 1000.0), 0.05),
        strong=AdaptationGroupConfig(650.0, 0.4, (100.0, 2000.0), 0.20),
        inhibitory_b=5.0,
        inhibitory_tau_w_ms=250.0,
    )
    cfg.inhibitory = NeuronPopulationConfig(tau_m_ms=15.0, refractory_ms=1.0)
    cfg.connectivity.ii_attenuation = 0.70
    return cfg.validate()


LEGACY_PRESETS: Dict[str, Callable[..., ReservoirConfig]] = {
    "legacy_ei_ssc": legacy_ei_ssc,
    "legacy_ei_0217": legacy_ei_0217,
    "legacy_ei_0215": legacy_ei_0215,
}


def nprc_historical_dt01(family: str = "B0", g_global: float = 7.0, master_seed: int = 0,
                         adaptation: str = "historical_b020") -> ReservoirConfig:
    """Reconstruction of the historical E/I model at dt = 0.1 ms.

    dt = 0.1 ms was inferred from the rate quantisation of historical results.  Corrected
    semantics (signs, orientation, exact refractory, delays >= 1 step); legacy
    per-step noise sigma = 0.05 (NOT dt-scaled); additive integration;
    historical_b020 adaptation; g_global scaling.  All intervals are in ms and
    converted to steps at dt = 0.1 (refractory 1 ms = 10 steps; delays sampled in
    ms, rounded to 0.1-ms steps, >= 1 step).
    B0 at dt = 0.1 ms means a 0.1-ms one-step synaptic transmission.
    """

    cfg = nprc_b0(g_global, adaptation, master_seed) if family == "B0" else nprc_b1(g_global, adaptation, master_seed)
    cfg.dt_ms = 0.1
    cfg.name = f"nprc_hist_dt01_{family.lower()}_{adaptation}"
    cfg.noise.sigma = 0.05
    cfg.noise.scaling = "per_step"
    return cfg.validate()


def avalanche_historical_pilot(family: str = "B0", g_global: float = 7.0, master_seed: int = 0,
                               duration_ms: float = 10000.0) -> AvalancheRunConfig:
    cfg = AvalancheRunConfig(reservoir=nprc_historical_dt01(family, g_global, master_seed),
                             duration_ms=duration_ms, burn_in_ms=1000.0)
    cfg.protocol_label = "reconstructed_nprc_historical_dt0.1ms"
    return cfg.validate()


def legacy_signonly_historical_dt01(family: str = "B1", g_global: float = 7.0, master_seed: int = 0) -> ReservoirConfig:
    """Causal sign-only control: identical to :func:`nprc_historical_dt01` except that
    the historical double-negative inhibitory forward pass is enabled (I spikes act
    excitatorily).  No other legacy flag is set."""

    cfg = nprc_historical_dt01(family, g_global, master_seed)
    cfg.name = f"legacy_signonly_{cfg.name}"
    cfg.legacy = LegacySemantics(double_negative_inhibition=True)
    return cfg.validate()


def avalanche_signonly_control(family: str = "B1", g_global: float = 7.0, master_seed: int = 0,
                               duration_ms: float = 10000.0) -> AvalancheRunConfig:
    cfg = avalanche_historical_pilot(family, g_global, master_seed, duration_ms)
    cfg.reservoir = legacy_signonly_historical_dt01(family, g_global, master_seed)
    cfg.protocol_label = "stage3.5_signonly_control_dt0.1ms"
    return cfg.validate()


def avalanche_pilot(
    family: str = "B0", g_global: float = 7.0, master_seed: int = 0, duration_ms: float = 10000.0
) -> AvalancheRunConfig:
    res = nprc_b0(g_global, master_seed=master_seed) if family == "B0" else nprc_b1(g_global, master_seed=master_seed)
    cfg = AvalancheRunConfig(reservoir=res, duration_ms=duration_ms, burn_in_ms=1000.0)
    return copy.deepcopy(cfg)
