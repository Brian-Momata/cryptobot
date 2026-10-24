import numpy as np
import pandas as pd
import pytest

from bot import strategies
from bot.data import synthetic_data
from bot.engine import CostModel, run_backtest
from bot.metrics import max_drawdown, round_trips, summarize, yearly_returns

NO_COSTS = CostModel(fee=0.0, slippage=0.0, min_rebalance=0.0)


@pytest.fixture(scope="module")
def df():
    return synthetic_data("1d", start="2018-01-01", end="2022-01-01", seed=7)


def make_df(prices):
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="D", tz="UTC")
    p = np.array(prices, dtype=float)
    return pd.DataFrame({"open": p, "high": p, "low": p, "close": p, "volume": 1.0}, index=idx)


# ---------- no lookahead: the most important test ----------

def assert_no_lookahead(strat, df):
    full = strat.target_positions(df)
    for cut in (300, 700, 1100):
        partial = strat.target_positions(df.iloc[:cut])
        pd.testing.assert_series_equal(full.iloc[:cut], partial, check_names=False)


@pytest.mark.parametrize("name", list(strategies.REGISTRY))
def test_no_lookahead(df, name):
    """A strategy's decision at candle t must not change when future candles are added."""
    assert_no_lookahead(strategies.make(name), df)


class PeekingStrategy(strategies.Strategy):
    """Cheats: buys today if tomorrow closes higher."""

    name = "peek"

    def target_positions(self, df):
        return (df["close"].shift(-1) > df["close"]).astype(float)


def test_lookahead_check_catches_a_cheat(df):
    """The lookahead test is only worth something if it fails on a strategy that peeks."""
    with pytest.raises(AssertionError):
        assert_no_lookahead(PeekingStrategy(), df)


@pytest.mark.parametrize("name", list(strategies.REGISTRY))
def test_targets_in_range(df, name):
    t = strategies.make(name).target_positions(df)
    assert t.between(0, 1).all()


# ---------- engine accounting ----------

def test_buy_and_hold_no_costs_matches_price():
    d = make_df([100, 110, 121, 90, 150])
    r = run_backtest(d, pd.Series(1.0, index=d.index), NO_COSTS, 1000)
    # Signal at candle 0 close executes at candle 1 open (110), then rides to 150.
    assert r.equity.iloc[0] == pytest.approx(1000)
    assert r.equity.iloc[-1] == pytest.approx(1000 * 150 / 110)


def test_executes_next_open_not_same_close():
    d = make_df([100, 100, 200, 200])
    target = pd.Series([0, 1, 1, 1], index=d.index, dtype=float)  # decide at close of candle 1
    r = run_backtest(d, target, NO_COSTS, 1000)
    assert r.trades.iloc[0]["time"] == d.index[2]  # filled at candle 2's open
    assert r.trades.iloc[0]["price"] == pytest.approx(200)
    assert r.equity.iloc[-1] == pytest.approx(1000)  # bought the jump, didn't profit from it


def test_fees_and_slippage_round_trip():
    d = make_df([100, 100, 100, 100])
    target = pd.Series([1, 0, 0, 0], index=d.index, dtype=float)
    costs = CostModel(fee=0.001, slippage=0.0005, min_rebalance=0.0)
    r = run_backtest(d, target, costs, 1000)
    assert len(r.trades) == 2
    # Flat price, so the whole loss is costs: about 2 x (0.1% + 0.05%).
    assert r.equity.iloc[-1] == pytest.approx(1000 * (1 - 0.0030), rel=2e-4)
    assert r.fees_paid > 0


def test_never_negative_cash_or_leverage(df):
    t = strategies.make("vol_target").target_positions(df)
    r = run_backtest(df, t, CostModel(), 1000)
    assert (r.position <= 1 + 1e-9).all()
    assert (r.equity > 0).all()


def test_min_rebalance_skips_tiny_trades():
    d = make_df([100] * 6)
    target = pd.Series([0.50, 0.51, 0.505, 0.5, 0.0, 0.0], index=d.index)
    r = run_backtest(d, target, CostModel(fee=0, slippage=0, min_rebalance=0.02), 1000)
    assert list(r.trades["side"]) == ["buy", "sell"]  # tiny tweaks ignored, full exit always done


def test_rejects_leverage():
    d = make_df([100, 100, 100])
    with pytest.raises(ValueError):
        run_backtest(d, pd.Series(1.5, index=d.index))


# ---------- metrics ----------

def test_max_drawdown():
    eq = pd.Series([100, 120, 60, 90, 130.0])
    assert max_drawdown(eq) == pytest.approx(-0.5)


def test_round_trips_and_win_rate():
    d = make_df([100, 100, 120, 120, 100, 100, 90, 90])
    target = pd.Series([1, 1, 0, 0, 1, 0, 0, 0], index=d.index, dtype=float)
    r = run_backtest(d, target, NO_COSTS, 1000)
    rt = round_trips(r)
    assert len(rt) == 2
    assert rt["return"].iloc[0] == pytest.approx(0.2)
    s = summarize(r, 365)
    assert s["round_trips"] == 2


def test_yearly_returns_compound_to_total(df):
    r = run_backtest(df, pd.Series(1.0, index=df.index), CostModel(), 1000)
    yr = yearly_returns(r.equity, 1000)
    assert (1 + yr).prod() - 1 == pytest.approx(r.equity.iloc[-1] / 1000 - 1)


def test_unknown_param_rejected():
    with pytest.raises(ValueError):
        strategies.make("sma_cross", fats=10)


def test_fixed_fraction_holds_constant_share():
    d = make_df([100, 200, 100, 200, 100])
    t = strategies.make("fixed_fraction", fraction=0.5).target_positions(d)
    r = run_backtest(d, t, NO_COSTS, 1000)
    # Rebalanced back to 50% at every open, so each close shows 50% marked at that same price.
    assert r.position.iloc[1:].tolist() == pytest.approx([0.5] * 4)


@pytest.mark.parametrize("name", ["buy_hold", "momentum", "sma_cross", "donchian"])
def test_engine_matches_independent_formula(df, name):
    """Recompute equity for all-in / all-out strategies with a plain formula, not the engine's
    loop: overnight gap, open-to-close move, and exact entry/exit cost factors."""
    fee, slip = 0.001, 0.0005
    t = strategies.make(name).target_positions(df).to_numpy()
    o, c = df["open"].to_numpy(), df["close"].to_numpy()
    held = np.r_[0.0, t[:-1]]  # position during candle i, set at its open
    prev = np.r_[0.0, held[:-1]]  # position just before that open
    r = np.ones(len(df))
    r[1:] = np.where(prev[1:] == 1, o[1:] / c[:-1], 1.0)
    r *= np.where((held == 1) & (prev == 0), 1 / ((1 + fee) * (1 + slip)), 1.0)
    r *= np.where((held == 0) & (prev == 1), (1 - fee) * (1 - slip), 1.0)
    r *= np.where(held == 1, c / o, 1.0)
    expected = 1000 * np.cumprod(r)
    eng = run_backtest(df, pd.Series(t, index=df.index), CostModel(fee=fee, slippage=slip), 1000)
    np.testing.assert_allclose(eng.equity.to_numpy(), expected, rtol=1e-10)
