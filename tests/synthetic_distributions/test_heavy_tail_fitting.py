"""Synthetic-distribution tests for the avalanche-size fitting protocol
(master plan 7.9; Clauset, Shalizi & Newman 2009)."""

import numpy as np
import pytest

from snnrc.criticality import powerlaw as pl
from snnrc.criticality.legacy_ols import gamma_ols_legacy


@pytest.fixture(scope="module")
def pl_sample():
    return pl.sample_discrete_power_law(20000, gamma=1.5, xmin=1, rng=np.random.default_rng(0))


def test_sampler_matches_exact_pmf():
    x = pl.sample_discrete_power_law(200000, gamma=2.2, xmin=3, rng=np.random.default_rng(1))
    emp = np.mean(x == 3)
    from scipy.special import zeta
    assert emp == pytest.approx(3 ** -2.2 / zeta(2.2, 3), rel=0.02)


@pytest.mark.parametrize("gamma,xmin", [(1.5, 1), (2.0, 1), (1.5, 5), (2.5, 2)])
def test_mle_recovers_known_gamma(gamma, xmin):
    x = pl.sample_discrete_power_law(20000, gamma, xmin, np.random.default_rng(2))
    fit = pl.fit_power_law(x, xmin)
    assert fit["gamma"] == pytest.approx(gamma, abs=0.03)
    assert fit["n_tail"] == 20000


def test_objective_xmin_selection_with_non_power_law_body():
    rng = np.random.default_rng(3)
    tail = pl.sample_discrete_power_law(6000, 1.8, 20, rng)
    body = rng.integers(1, 20, 6000)          # uniform body below xmin
    scan = pl.scan_xmin(np.r_[body, tail], min_tail=200, min_distinct=10)
    assert 15 <= scan["best"]["xmin"] <= 26
    assert scan["best"]["gamma"] == pytest.approx(1.8, abs=0.08)
    assert len(scan["table"]) > 5 and all("ks" in r for r in scan["table"])


def test_ks_small_for_true_model_large_for_wrong(pl_sample):
    good = pl.ks_distance_power_law(pl_sample, 1.5, 1)
    bad = pl.ks_distance_power_law(pl_sample, 2.2, 1)
    assert good < 0.015 < bad


def test_bootstrap_reproducible_and_covers_truth(pl_sample):
    a = pl.bootstrap_gamma_ci(pl_sample, xmin=1, n_boot=60, seed=7)
    b = pl.bootstrap_gamma_ci(pl_sample, xmin=1, n_boot=60, seed=7)
    assert a == b
    assert a["ci_low"] < 1.5 < a["ci_high"]


def test_gof_pvalue_reproducible_and_not_rejecting_true_model():
    x = pl.sample_discrete_power_law(3000, 1.6, 1, np.random.default_rng(4))
    kw = dict(n_boot=40, seed=11, min_tail=100, min_distinct=5, max_candidates=8)
    a = pl.gof_pvalue(x, **kw); b = pl.gof_pvalue(x, **kw)
    assert a == b
    assert a["p_value"] > 0.1


def test_gof_rejects_exponential_data():
    x = 1 + np.random.default_rng(5).geometric(0.15, 3000) - 1
    res = pl.gof_pvalue(x, n_boot=40, seed=1, min_tail=100, min_distinct=5, max_candidates=8)
    assert res["p_value"] < 0.1


def test_truncated_power_law_recovery_and_preference():
    x = pl.sample_truncated_power_law(20000, gamma=1.5, s_c=200.0, xmin=1, rng=np.random.default_rng(6))
    tr = pl.fit_truncated_power_law(x, 1)
    assert tr["gamma"] == pytest.approx(1.5, abs=0.05)
    assert tr["s_c"] == pytest.approx(200.0, rel=0.25)
    cmp = pl.compare_power_law_vs_truncated(x, 1)
    assert cmp["preferred"] == "truncated_power_law" and cmp["p_value"] < 1e-3


def test_pure_power_law_not_falsely_truncated(pl_sample):
    cmp = pl.compare_power_law_vs_truncated(pl_sample, 1)
    assert cmp["p_value"] > 0.01


def test_exponential_data_prefers_exponential():
    x = 2 + np.random.default_rng(8).geometric(0.1, 5000) - 1
    c = pl.compare_models(x, 2, "power_law", "exponential")
    assert c["R"] < 0 and c["p_value"] < 0.01


def test_lognormal_data_prefers_lognormal():
    rng = np.random.default_rng(9)
    x = np.maximum(1, np.round(rng.lognormal(3.0, 0.6, 8000))).astype(int)
    c = pl.compare_models(x, int(np.percentile(x, 10)), "power_law", "lognormal")
    assert c["R"] < 0 and c["p_value"] < 0.01


def test_power_law_data_prefers_power_law_over_exponential(pl_sample):
    c = pl.compare_models(pl_sample, 1, "power_law", "exponential")
    assert c["R"] > 0 and c["p_value"] < 0.01


def test_full_protocol_record_and_ols_is_only_legacy_column():
    x = pl.sample_truncated_power_law(5000, 1.5, 300.0, 1, np.random.default_rng(10))
    rec = pl.fit_avalanche_sizes(x, n_boot_ci=30, n_boot_gof=20, seed=3, min_tail=100, min_distinct=10)
    required = {"gamma_MLE", "gamma_ci_low", "gamma_ci_high", "S_min", "S_max", "S_c", "ks_statistic",
                "gof_p_value", "n_tail", "fitted_range", "fitted_range_decades", "log_likelihood",
                "model_comparison", "bootstrap_seed", "bootstrap_count_ci", "bootstrap_count_gof",
                "gamma_OLS_legacy", "primary_estimator", "xmin_scan_table"}
    assert required <= set(rec)
    assert rec["primary_estimator"] == "discrete_MLE_KS_xmin"
    assert rec["S_max"] is None                       # no arbitrary upper cut
    ols = gamma_ols_legacy(x)
    assert rec["gamma_OLS_legacy"]["gamma"] == pytest.approx(ols["gamma"])
    assert set(rec["model_comparison"]) >= {"power_law_vs_truncated", "power_law_vs_exponential",
                                            "power_law_vs_lognormal", "truncated_vs_lognormal"}


def test_ols_legacy_is_biased_on_truncated_data():
    x = pl.sample_truncated_power_law(20000, 1.5, 100.0, 1, np.random.default_rng(12))
    assert abs(gamma_ols_legacy(x)["gamma"] - 1.5) > 0.2


def test_sampler_near_gamma_one_never_overflows():
    x = pl.sample_discrete_power_law(20000, 1.000001, 1, np.random.default_rng(0), table_size=1000)
    assert x.min() >= 1 and x.max() <= pl.SAMPLE_CAP
