"""Spec tests: E-only mean-IEI binned avalanche definition (Stage 3.5 primary)."""

import numpy as np
import pytest

from snnrc.criticality.binned import (bin_steps_for, binned_counts, branching_diagnostics,
                                      mean_population_iei_steps, segment_binned)


def test_mean_iei_of_merged_train_counts_simultaneous_events():
    # events at steps 0, 0, 4, 10 -> intervals 0, 4, 6 -> mean 10/3
    assert mean_population_iei_steps(np.array([10, 0, 4, 0])) == pytest.approx(10 / 3)


@pytest.mark.parametrize("m,iei,expected", [(1.0, 8.3, 8), (0.5, 8.3, 4), (2.0, 8.3, 17),
                                             (1.0, 0.2, 1), (1.0, 2.5, 3), (0.5, 3.0, 2)])
def test_deterministic_rounding_round_half_up_min_one(m, iei, expected):
    assert bin_steps_for(m, iei) == expected


def test_binning_aligned_to_start_and_partial_bin_dropped():
    c = binned_counts(np.array([5, 9, 10, 14, 15, 29]), start_step=5, end_step=30, bin_steps=5)
    assert c.tolist() == [2, 2, 1, 0, 1]          # bins [5,10) [10,15) [15,20) [20,25) [25,30)
    c2 = binned_counts(np.array([5, 30, 33]), start_step=5, end_step=34, bin_steps=5)
    assert c2.tolist() == [1, 0, 0, 0, 0]         # [30,34) is a partial bin -> 30 and 33 dropped


def test_avalanches_are_runs_bounded_by_empty_bins():
    counts = np.array([0, 2, 1, 0, 3, 0, 0, 1, 1, 1, 0])
    seg = segment_binned(counts)
    assert [(r["start_bin"], r["end_bin"], r["size"], r["duration_bins"]) for r in seg["table"]] == \
        [(1, 2, 3, 2), (4, 4, 3, 1), (7, 9, 3, 3)]
    assert seg["excluded_edge"] == 0


def test_edge_runs_without_bounding_empty_bin_are_excluded_and_counted():
    seg = segment_binned(np.array([1, 1, 0, 2, 0, 5]))
    assert [r["size"] for r in seg["table"]] == [2] and seg["excluded_edge"] == 2


def test_one_empty_bin_closes():
    seg = segment_binned(np.array([0, 1, 0, 1, 0]))
    assert len(seg["table"]) == 2


def test_branching_first_bin_and_ratio():
    counts = np.array([0, 2, 4, 0, 0, 3, 0])
    b = branching_diagnostics(counts)
    assert b["sigma_first_bin"] == pytest.approx((4 / 2 + 0 / 3) / 2)
    # ratios: aval1 4/2, 0/4 ; aval2 0/3  -> mean (2 + 0 + 0)/3
    assert b["sigma_bin_ratio"] == pytest.approx(2 / 3)
    assert b["role"] == "diagnostic_only"


def test_branching_estimator_on_synthetic_branching_process():
    """Validation fixture: a Galton-Watson process with Poisson(m) offspring,
    one seed per avalanche, binned at the generation time scale."""

    rng = np.random.default_rng(0)
    for m in (0.5, 0.9):
        counts = []
        for _ in range(4000):
            counts.append(0)
            n = 1
            while n and len(counts) < 10_000_000:
                counts.append(n)
                n = rng.poisson(m * n)
        counts.append(0)
        b = branching_diagnostics(np.array(counts))
        assert b["sigma_first_bin"] == pytest.approx(m, abs=0.05)
