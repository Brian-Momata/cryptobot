import pandas as pd
import pytest

from bot import strategies
from bot.data import synthetic_data
from bot.engine import CostModel, run_backtest
from bot.robustness import exposure_matched, neighbour_median, rank_corr, share_beating


def grid_table(values):
    """2-D grid a in (1,2,3) x b in (10,20,30), metric from a row-major list."""
    rows = [{"a": a, "b": b} for a in (1, 2, 3) for b in (10, 20, 30)]
    t = pd.DataFrame(rows)
    t["m"] = values
    return t


def test_neighbour_median_one_dimension():
    t = pd.DataFrame({"lookback": [30, 60, 90, 120], "m": [1.0, 2.0, 9.0, 4.0]})
    nbr = neighbour_median(t, ["lookback"], "m")
    assert list(nbr) == [1.5, 2.0, 4.0, 6.5]  # ends have one neighbour, middle two


def test_neighbour_median_uses_sorted_steps_not_row_order():
    t = pd.DataFrame({"lookback": [120, 30, 90, 60], "m": [4.0, 1.0, 9.0, 2.0]})
    nbr = neighbour_median(t, ["lookback"], "m")
    assert nbr.loc[2] == 4.0  # lookback 90: neighbours 60 (2) and 120 (4), itself 9


def test_neighbour_median_lone_spike_is_flattened():
    # Centre is a spike, everything else poor: its neighbourhood score should be poor.
    t = grid_table([0, 0, 0, 0, 5, 0, 0, 0, 0])
    nbr = neighbour_median(t, ["a", "b"], "m")
    assert nbr.loc[4] == 0  # centre + 4 orthogonal neighbours, diagonals excluded


def test_neighbour_median_excludes_diagonals():
    t = grid_table([7, 0, 7, 0, 1, 0, 7, 0, 7])  # corners high, edges low
    nbr = neighbour_median(t, ["a", "b"], "m")
    assert nbr.loc[4] == 0


def test_rank_corr_sign():
    t = pd.DataFrame({"IS_sharpe": [1, 2, 3, 4], "OOS_sharpe": [0.1, 0.2, 0.3, 0.4]})
    assert rank_corr(t) == pytest.approx(1.0)
    t["OOS_sharpe"] = t["OOS_sharpe"][::-1].to_numpy()
    assert rank_corr(t) == pytest.approx(-1.0)


def test_share_beating():
    t = pd.DataFrame({"OOS_sharpe": [0.5, 0.7, 0.9, 1.1]})
    assert share_beating(t, "OOS_sharpe", 0.69) == 0.75


def test_exposure_matched_holds_strategy_average_exposure():
    df = synthetic_data("1d", start="2018-01-01", end="2021-01-01", seed=3)
    strat = run_backtest(df, strategies.make("vol_target").target_positions(df), CostModel(), 1000)
    bench = exposure_matched(df, strat, CostModel(), 1000)
    # Benchmark's first candle is cash (fills happen next open), so compare from candle 1 on.
    assert bench.position.iloc[1:].mean() == pytest.approx(strat.position.mean(), abs=0.02)
    assert bench.label.startswith("fixed_fraction(")
    assert len(bench.trades) > 1  # it rebalances, so its fees are counted too
