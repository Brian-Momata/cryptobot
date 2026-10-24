"""Robustness checks for parameter sweeps.

A setting is only worth trusting if its neighbours on the grid also work. A lone spike
surrounded by bad settings is usually luck.
"""
from __future__ import annotations

import pandas as pd

from .engine import BacktestResult, CostModel, run_backtest
from .strategies import FixedFraction


def neighbour_median(table: pd.DataFrame, param_cols: list[str], metric: str) -> pd.Series:
    """For each row, the median of `metric` over itself and its grid neighbours.

    Neighbours are rows that differ by one step (in the sorted list of tested values) in
    exactly one parameter, with every other parameter equal.
    """
    steps = {c: {v: i for i, v in enumerate(sorted(table[c].unique()))} for c in param_cols}
    pos = pd.DataFrame({c: table[c].map(steps[c]) for c in param_cols}, index=table.index)
    out = {}
    for idx in table.index:
        dist = (pos - pos.loc[idx]).abs()
        near = (dist.sum(axis=1) <= 1) & (dist.max(axis=1) <= 1)
        out[idx] = table.loc[near, metric].median()
    return pd.Series(out, name=f"{metric}_nbr").reindex(table.index)


def rank_corr(table: pd.DataFrame, a: str = "IS_sharpe", b: str = "OOS_sharpe") -> float:
    """Spearman rank correlation: do settings that ranked well in-sample also rank well out-of-sample?"""
    # Spearman = Pearson on ranks. Done by hand because pandas' method="spearman" needs scipy.
    return float(table[a].rank().corr(table[b].rank()))


def share_beating(table: pd.DataFrame, metric: str, benchmark: float) -> float:
    """Fraction of all tested settings whose `metric` beats the benchmark's."""
    return float((table[metric] > benchmark).mean())


def exposure_matched(df: pd.DataFrame, result: BacktestResult, costs: CostModel | None = None,
                     initial_cash: float = 1_000.0) -> BacktestResult:
    """Buy & hold scaled down to the strategy's average exposure, rebalanced to a fixed fraction.

    A strategy that sits in cash half the time gets about half of buy & hold's drawdown for
    free. This is the fair benchmark for that: same average exposure, no timing.
    """
    frac = round(float(result.position.reindex(df.index).mean()), 2)
    strat = FixedFraction(fraction=frac)
    return run_backtest(df, strat.target_positions(df), costs, initial_cash, strat.label())
