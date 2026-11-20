"""Named, independent random streams.

Each named seed drives its own generator, so changing e.g. the noise seed can
never alter the topology.  Unset seeds are derived deterministically from the
master seed with :class:`numpy.random.SeedSequence` (spawn key = stream name),
and the resolved integers are written into every manifest.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Dict

import numpy as np
import torch

from .config import SeedConfig

STREAMS = (
    "topology",
    "neuron_params",
    "delays",
    "input_weights",
    "noise",
    "kick",
    "data",
    "readout",
)


def _stream_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:4], "little")


def resolve_seeds(seeds: SeedConfig) -> Dict[str, int]:
    """Return every stream's integer seed (explicit value or master-derived)."""

    out: Dict[str, int] = {"master": int(seeds.master)}
    for name in STREAMS:
        value = getattr(seeds, name)
        if value is None:
            ss = np.random.SeedSequence([int(seeds.master), _stream_key(name)])
            value = int(ss.generate_state(1, dtype=np.uint32)[0])
        out[name] = int(value)
    return out


def resolved_seed_config(seeds: SeedConfig) -> SeedConfig:
    return SeedConfig(**resolve_seeds(seeds))


def numpy_rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(int(seed))


def torch_generator(seed: int, device: str | torch.device = "cpu") -> torch.Generator:
    g = torch.Generator(device=device)
    g.manual_seed(int(seed) % (2**63))
    return g


def per_sample_seed(noise_seed: int, sample_key: int) -> int:
    """Deterministic seed for one task sample (same reservoir/sample/seed -> same noise)."""

    ss = np.random.SeedSequence([int(noise_seed), int(sample_key)])
    return int(ss.generate_state(1, dtype=np.uint64)[0] % (2**63))


def seed_dict(seeds: SeedConfig) -> Dict[str, int | None]:
    return dataclasses.asdict(seeds)
