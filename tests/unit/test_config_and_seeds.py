"""Spec tests: configs, legacy-name guard, seeds, fingerprints."""

import dataclasses

import pytest

from snnrc.config import AvalancheRunConfig, LegacySemantics, load_config, save_config, to_dict
from snnrc.presets import LEGACY_PRESETS, avalanche_pilot, nprc_b0, nprc_b1
from snnrc.seeding import resolve_seeds


def test_yaml_round_trip(tmp_path):
    cfg = avalanche_pilot("B1", 7.1, master_seed=3)
    save_config(cfg, tmp_path / "c.yaml")
    back = load_config(AvalancheRunConfig, tmp_path / "c.yaml")
    assert to_dict(back) == to_dict(cfg)


def test_scientific_presets_have_no_legacy_semantics():
    for cfg in (nprc_b0(7.0), nprc_b1(7.0), nprc_b0(7.0, "historical_b015")):
        assert not cfg.legacy.any() and cfg.semantics_label == "corrected"
        assert cfg.dt_ms == 1.0 and cfg.N_E == 800 and cfg.N_I == 200
        assert cfg.adaptation.inhibitory_b == 0.0


def test_baseline_adaptation_is_b020_with_b015_ablation():
    assert nprc_b0().adaptation.strong.b == 0.20 and nprc_b0().adaptation.weak.b == 0.05
    assert nprc_b0(adaptation="historical_b015").adaptation.strong.b == 0.15


@pytest.mark.parametrize("name", list(LEGACY_PRESETS))
def test_legacy_presets_are_named_and_flagged(name):
    cfg = LEGACY_PRESETS[name]()
    assert cfg.name.startswith("legacy_") and cfg.legacy.any() and cfg.semantics_label == "legacy"


def test_legacy_flags_refused_in_unnamed_config():
    cfg = nprc_b0(1.0)
    cfg.legacy = LegacySemantics(ee_transposed=True)
    with pytest.raises(ValueError, match="legacy_"):
        cfg.validate()


def test_b0_cannot_have_delay():
    cfg = nprc_b1(1.0)
    cfg.family = "B0"
    with pytest.raises(ValueError):
        cfg.validate()


def test_seed_streams_independent_and_deterministic():
    a = resolve_seeds(nprc_b0(master_seed=1).seeds)
    b = resolve_seeds(nprc_b0(master_seed=1).seeds)
    assert a == b and len(set(v for k, v in a.items() if k != "master")) == 8
    cfg = nprc_b0(master_seed=1); cfg.seeds.noise = 99
    c = resolve_seeds(cfg.seeds)
    assert c["noise"] == 99 and {k: v for k, v in c.items() if k != "noise"} == {k: v for k, v in a.items() if k != "noise"}


def test_no_ambiguous_J_E_field():
    names = {f.name for f in dataclasses.fields(nprc_b0().scaling)}
    assert "J_E" not in names and {"g_global", "J_E_exc", "J_I"} <= names
