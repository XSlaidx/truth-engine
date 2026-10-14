"""The statistical layer must be verifiable against the papers' own worked numbers."""
import math
import random
import statistics as st
from statistics import NormalDist

import pytest

import stats

NORM = NormalDist()


def test_psr_at_the_threshold_is_one_half():
    """If the observed Sharpe equals the threshold, the confidence is exactly 50%."""
    assert stats.probabilistic_sharpe_ratio(0.1, 100, 0.0, 3.0, sr_threshold=0.1) == pytest.approx(0.5)


def test_psr_matches_its_formula_for_normal_returns():
    sr, n = 0.12, 250
    expected = NORM.cdf(sr * math.sqrt(n - 1) / math.sqrt(1 - 0 + ((3.0 - 1) / 4) * sr ** 2))
    assert stats.probabilistic_sharpe_ratio(sr, n, 0.0, 3.0) == pytest.approx(expected)


def test_psr_grows_with_sample_length_and_shrinks_with_the_bar():
    short = stats.probabilistic_sharpe_ratio(0.1, 30, 0.0, 3.0)
    long = stats.probabilistic_sharpe_ratio(0.1, 300, 0.0, 3.0)
    assert long > short
    assert stats.probabilistic_sharpe_ratio(0.1, 300, 0.0, 3.0, sr_threshold=0.2) < long


def test_expected_max_sharpe_grows_with_trials_and_matches_monte_carlo():
    assert stats.expected_max_sharpe(1, 0.04) == 0.0
    assert stats.expected_max_sharpe(50, 0.04) > stats.expected_max_sharpe(10, 0.04) > 0
    assert stats.expected_max_sharpe(10, 0.09) > stats.expected_max_sharpe(10, 0.04)

    # Monte Carlo check: draw N Sharpe estimates from N(0, V) many times, average the maximum
    rng = random.Random(7)
    n_trials, var = 20, 0.04
    sigma = math.sqrt(var)
    maxima = [max(rng.gauss(0, sigma) for _ in range(n_trials)) for _ in range(4000)]
    closed_form = stats.expected_max_sharpe(n_trials, var)
    assert abs(st.fmean(maxima) - closed_form) / closed_form < 0.05


def test_dsr_without_trials_equals_psr_and_falls_as_trials_grow():
    sr, n = 0.15, 200
    plain = stats.probabilistic_sharpe_ratio(sr, n, 0.0, 3.0)
    alone = stats.deflated_sharpe_ratio(sr, n, 0.0, 3.0, n_trials=1, var_sr=0.0)
    assert alone["dsr"] == pytest.approx(plain)

    many = stats.deflated_sharpe_ratio(sr, n, 0.0, 3.0, n_trials=500, var_sr=0.04)
    few = stats.deflated_sharpe_ratio(sr, n, 0.0, 3.0, n_trials=5, var_sr=0.04)
    assert many["dsr"] < few["dsr"] < plain
    assert many["threshold"] > few["threshold"] > 0


def test_min_backtest_length_matches_the_paper_anchor():
    """Bailey et al.: with 5 years of data no more than ~45 configurations should be tried."""
    assert stats.min_backtest_length_years(45, 1.0) == pytest.approx(5.0, abs=0.15)
    assert stats.min_backtest_length_years(200, 1.0) > stats.min_backtest_length_years(20, 1.0)
    assert stats.min_backtest_length_years(50, 2.0) < stats.min_backtest_length_years(50, 1.0)
    assert stats.min_backtest_length_years(1, 1.0) == 0.0


def noise_matrix(rng, periods=40, trials=8, scale=0.01):
    return [[rng.gauss(0, scale) for _ in range(trials)] for _ in range(periods)]


def test_pbo_is_zero_when_one_trial_genuinely_dominates():
    rng = random.Random(11)
    matrix = noise_matrix(rng)
    for row in matrix:                      # trial 0: positive in every period, others are noise
        row[0] = abs(row[0]) + 0.01
    report = stats.pbo_cscv(matrix, groups=8)
    assert report["pbo"] == 0.0
    assert report["median_logit"] > 0


def test_pbo_flags_a_regime_lucky_trial():
    """A trial that only looks good in one half of the sample is what overfitting looks like."""
    rng = random.Random(23)
    periods, trials = 60, 6
    matrix = noise_matrix(rng, periods=periods, trials=trials)
    for t, row in enumerate(matrix):
        row[0] = 0.02 + rng.gauss(0, 0.002) if t < periods // 2 else -0.02 + rng.gauss(0, 0.002)
    report = stats.pbo_cscv(matrix, groups=10)
    assert 0.0 < report["pbo"] <= 1.0
    assert report["pbo"] > 0.2


def test_pbo_rejects_degenerate_arguments():
    with pytest.raises(ValueError):
        stats.pbo_cscv([[0.01, 0.02]])
    with pytest.raises(ValueError):
        stats.pbo_cscv([[0.01, 0.02]] * 10, groups=7)
    with pytest.raises(ValueError):
        stats.pbo_cscv([[0.01]] * 10, groups=4)


def test_moments_of_a_normal_sample_are_close_to_normal():
    rng = random.Random(3)
    sample = [rng.gauss(0, 0.01) for _ in range(20000)]
    mom = stats.moments(sample)
    assert abs(mom["skew"]) < 0.1
    assert abs(mom["kurt"] - 3.0) < 0.2


def test_gate_rejects_a_negative_edge_with_reasons():
    rng = random.Random(5)
    returns = [rng.gauss(-0.001, 0.01) for _ in range(40)]
    verdict = stats.evaluate_gate(returns, n_trials=3, var_sr=0.01, weeks=6)
    assert verdict.passed is False
    assert any("mean result" in reason for reason in verdict.reasons)
    assert "dsr" in verdict.numbers


def test_gate_passes_a_strong_clean_edge():
    returns = [0.004 + (i % 3) * 0.0001 for i in range(60)]     # steady, low-variance gains
    verdict = stats.evaluate_gate(returns, n_trials=1, var_sr=0.0, weeks=8, min_trades=30)
    assert verdict.passed is True, verdict.summary()
    assert verdict.numbers["psr"] > 0.95


def test_gate_refuses_to_pass_on_too_little_data():
    verdict = stats.evaluate_gate([0.01, 0.02, 0.03], n_trials=1, var_sr=0.0, weeks=1)
    assert verdict.passed is False
    assert any("closed trades" in reason for reason in verdict.reasons)
    assert any("weeks" in reason for reason in verdict.reasons)
