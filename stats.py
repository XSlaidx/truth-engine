"""stats.py — statistical honesty layer: PSR, Deflated Sharpe Ratio, PBO (CSCV), MinBTL.

The problem this file exists for: a Sharpe ratio printed from a backtest is not evidence. Bailey,
Borwein, López de Prado and Zhu showed that after only seven tried configurations the expected
in-sample Sharpe of a *skill-less* strategy on two years of data is about 1, while its expected
out-of-sample Sharpe is zero. So the number that matters is not "my Sharpe is 1.4" but "my Sharpe is
1.4 after accounting for how many things I tried, how short the sample is, and how non-normal the
returns are".

Implemented from the primary sources (no dependencies beyond the standard library):

* Probabilistic Sharpe Ratio (PSR) and Deflated Sharpe Ratio (DSR) — Bailey & López de Prado,
  "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality";
* Probability of Backtest Overfitting (PBO) via combinatorially symmetric cross-validation (CSCV) —
  Bailey, Borwein, López de Prado, Zhu, "The Probability of Backtest Overfitting";
* Minimum Backtest Length (MinBTL) — same authors, "Pseudo-Mathematics and Financial Charlatanism".

Every function takes returns as *per-period* fractions (a +0.01 is +1%). Sharpe ratios are per-period
unless you pass `periods_per_year` explicitly; mixing units is the classic way to fool yourself.
"""
from __future__ import annotations

import itertools
import math
import statistics as st
from dataclasses import dataclass, field
from statistics import NormalDist

EULER_GAMMA = 0.5772156649015329
_NORM = NormalDist()


def moments(returns: list[float]) -> dict:
    """Mean, standard deviation, skewness and (non-excess) kurtosis of a return series."""
    n = len(returns)
    if n < 2:
        raise ValueError("need at least two observations")
    mean = st.fmean(returns)
    m2 = st.fmean([(r - mean) ** 2 for r in returns])
    if m2 == 0:
        return {"mean": mean, "std": 0.0, "skew": 0.0, "kurt": 3.0, "n": n}
    m3 = st.fmean([(r - mean) ** 3 for r in returns])
    m4 = st.fmean([(r - mean) ** 4 for r in returns])
    return {"mean": mean, "std": math.sqrt(m2), "skew": m3 / m2 ** 1.5,
            "kurt": m4 / m2 ** 2, "n": n}


def sharpe(returns: list[float], periods_per_year: float | None = None) -> float:
    """Per-period Sharpe by default; annualised only when the period count is stated explicitly."""
    if len(returns) < 2:
        raise ValueError("need at least two observations")
    mean, std = st.fmean(returns), st.pstdev(returns)
    if std == 0:
        return 0.0
    sr = mean / std
    return sr * math.sqrt(periods_per_year) if periods_per_year else sr


def probabilistic_sharpe_ratio(sr: float, n: int, skew: float, kurt: float,
                               sr_threshold: float = 0.0) -> float:
    """Probability that the true Sharpe exceeds `sr_threshold`, given sample length and moments.

    PSR = Φ[ (SR - SR*) · √(n-1) / √(1 - γ3·SR + ((γ4-1)/4)·SR²) ] with γ3 skew and γ4 kurtosis
    (kurtosis of a Normal is 3, not 0 — pass non-excess kurtosis).
    """
    if n < 2:
        raise ValueError("need at least two observations")
    denominator = 1 - skew * sr + ((kurt - 1) / 4) * sr ** 2
    if denominator <= 0:
        raise ValueError("degenerate moments: variance of the Sharpe estimator is not positive")
    z = (sr - sr_threshold) * math.sqrt(n - 1) / math.sqrt(denominator)
    return _NORM.cdf(z)


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """Expected maximum Sharpe among `n_trials` skill-less trials with the given SR variance.

    E[max SR] ≈ √V · [ (1-γ)·Φ⁻¹(1 - 1/N) + γ·Φ⁻¹(1 - 1/(N·e)) ] — the winner's curse in closed form.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if n_trials == 1:
        return 0.0
    if var_sr <= 0:
        return 0.0
    first = _NORM.inv_cdf(1 - 1 / n_trials)
    second = _NORM.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * first + EULER_GAMMA * second)


def deflated_sharpe_ratio(sr: float, n: int, skew: float, kurt: float, *,
                          n_trials: int, var_sr: float) -> dict:
    """DSR = PSR with the threshold raised to the expected maximum under multiple testing."""
    threshold = expected_max_sharpe(n_trials, var_sr)
    return {"dsr": probabilistic_sharpe_ratio(sr, n, skew, kurt, threshold),
            "threshold": threshold, "n_trials": n_trials, "var_sr": var_sr}


def min_backtest_length_years(n_trials: int, expected_max_sr: float) -> float:
    """Years of data needed so that N trials do not produce a Sharpe of `expected_max_sr` by luck."""
    if expected_max_sr <= 0:
        raise ValueError("expected max Sharpe must be positive")
    if n_trials < 2:
        return 0.0
    first = _NORM.inv_cdf(1 - 1 / n_trials)
    second = _NORM.inv_cdf(1 - 1 / (n_trials * math.e))
    return ((1 - EULER_GAMMA) * first + EULER_GAMMA * second) ** 2 / expected_max_sr ** 2


def pbo_cscv(matrix: list[list[float]], groups: int = 10) -> dict:
    """Probability of backtest overfitting, CSCV.

    `matrix[t][j]` is the return of trial j in period t. Rows are split into `groups` (even) blocks;
    for every balanced IS/OOS split we take the trial that is best in-sample and look at where it
    ranks out-of-sample. If the IS winner systematically lands below the OOS median, the selection
    process is overfitting and PBO approaches 1.
    """
    if not matrix or not matrix[0]:
        raise ValueError("empty return matrix")
    if groups % 2 or groups < 2:
        raise ValueError("groups must be an even number >= 2")
    trials = len(matrix[0])
    if trials < 2:
        raise ValueError("need at least two trials to speak about selection")
    periods = len(matrix)
    if groups > periods:
        raise ValueError("more groups than periods")

    blocks = [matrix[i * periods // groups:(i + 1) * periods // groups] for i in range(groups)]
    logits: list[float] = []
    for chosen in itertools.combinations(range(groups), groups // 2):
        is_rows = [row for index in chosen for row in blocks[index]]
        oos_rows = [row for index in range(groups) if index not in chosen for row in blocks[index]]
        if not is_rows or not oos_rows:
            continue
        is_sr = [_trial_sharpe(is_rows, j) for j in range(trials)]
        best = max(range(trials), key=lambda j: is_sr[j])
        oos_sr = sorted(_trial_sharpe(oos_rows, j) for j in range(trials))
        rank = oos_sr.index(_trial_sharpe(oos_rows, best))
        omega = (rank + 1) / (trials + 1)          # relative rank of the IS winner out of sample
        omega = min(max(omega, 1e-9), 1 - 1e-9)
        logits.append(math.log(omega / (1 - omega)))
    if not logits:
        raise ValueError("no valid split produced")
    overfit = sum(1 for value in logits if value <= 0) / len(logits)
    return {"pbo": overfit, "splits": len(logits),
            "median_logit": st.median(logits), "mean_logit": st.fmean(logits)}


def _trial_sharpe(rows: list[list[float]], trial: int) -> float:
    series = [row[trial] for row in rows]
    std = st.pstdev(series) if len(series) > 1 else 0.0
    return st.fmean(series) / std if std else 0.0


@dataclass
class GateVerdict:
    """Outcome of the readiness gate: pass, or the concrete reasons it did not."""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    numbers: dict = field(default_factory=dict)

    def summary(self) -> str:
        head = "GATE: PASS" if self.passed else "GATE: NOT PASSED"
        lines = [head]
        for key, value in self.numbers.items():
            lines.append(f"  {key}: {value}")
        lines += [f"  - {reason}" for reason in self.reasons]
        return "\n".join(lines)


def evaluate_gate(returns: list[float], *, n_trials: int, var_sr: float, weeks: float,
                  min_trades: int = 30, min_weeks: float = 4.0, psr_threshold: float = 0.95,
                  pbo_limit: float = 0.5, trial_matrix: list[list[float]] | None = None) -> GateVerdict:
    """The whole statistical picture in one call — used by the stand before any real money."""
    reasons: list[str] = []
    numbers: dict = {"trades": len(returns), "weeks": round(weeks, 1), "trials": n_trials}
    if len(returns) < 2:
        return GateVerdict(False, ["too few closed trades to compute anything"], numbers)

    mom = moments(returns)
    sr = sharpe(returns)
    psr = probabilistic_sharpe_ratio(sr, mom["n"], mom["skew"], mom["kurt"])
    dsr = deflated_sharpe_ratio(sr, mom["n"], mom["skew"], mom["kurt"],
                                n_trials=n_trials, var_sr=var_sr)
    numbers.update({"sharpe_per_trade": round(sr, 4), "psr": round(psr, 4),
                    "dsr": round(dsr["dsr"], 4), "dsr_threshold": round(dsr["threshold"], 4),
                    "skew": round(mom["skew"], 3), "kurtosis": round(mom["kurt"], 3)})
    if len(returns) < min_trades:
        reasons.append(f"only {len(returns)} closed trades (< {min_trades})")
    if weeks < min_weeks:
        reasons.append(f"only {weeks:.1f} weeks of forward testing (< {min_weeks})")
    if st.fmean(returns) <= 0:
        reasons.append("mean result per trade is not positive")
    if psr < psr_threshold:
        reasons.append(f"PSR {psr:.3f} below {psr_threshold} — the edge is not distinguishable from luck")
    if dsr["dsr"] < psr_threshold:
        reasons.append(f"DSR {dsr['dsr']:.3f} below {psr_threshold} after deflating for {n_trials} trials")
    if trial_matrix:
        pbo = pbo_cscv(trial_matrix)
        numbers["pbo"] = round(pbo["pbo"], 3)
        if pbo["pbo"] > pbo_limit:
            reasons.append(f"PBO {pbo['pbo']:.2f} — the selection process looks overfit")
    return GateVerdict(not reasons, reasons, numbers)
