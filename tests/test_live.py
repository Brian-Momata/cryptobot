import numpy as np
import pandas as pd
import pytest

from bot.data import synthetic_data
from bot.engine import CostModel, run_backtest
from bot.live.broker import Fill, assert_testnet, fill_from_ccxt, retry
from bot.live.config import Settings, read_dotenv
from bot.live.fake import FakeBroker, FakeMarket, RecordingAlerts
from bot.live.risk import Limits, OrderPlan, check_order, plan_order
from bot.live.state import State
from bot.live.trader import OUTAGE_ALERT_AFTER, OUTAGE_REALERT_EVERY, Trader
from bot.strategies import VolTargetBlend, VolTargetTrend

NO_LIMITS = Limits(soft_daily_loss=-1, hard_daily_loss=-1, max_drawdown=-1)


class Clock:
    def __init__(self, t):
        t = pd.Timestamp(t)
        self.t = t if t.tzinfo else t.tz_localize("UTC")

    def __call__(self):
        return self.t


@pytest.fixture(scope="module")
def candles():
    return {"BTC/USDT": synthetic_data("1d", start="2018-01-01", end="2020-06-01", seed=3),
            "ETH/USDT": synthetic_data("1d", start="2018-01-01", end="2020-06-01", seed=4, start_price=500)}


def make_trader(tmp_path, candles, start, limits=NO_LIMITS, min_notional=0.0, **broker_kw):
    clock = Clock(start)
    market = FakeMarket(candles, clock)
    broker = FakeBroker({"BTC": 1.0, "USDT": 10_000.0}, min_notional=min_notional, **broker_kw)
    settings = Settings(allocated_usdt=1_000.0, db_path=tmp_path / "s.sqlite", kill_path=tmp_path / "KILL")
    alerts = RecordingAlerts()
    t = Trader(settings, broker, market, State(":memory:"), alerts, clock=clock, limits=limits,
               log=lambda *_: None)
    sync_price(t)
    t.init()
    return t


def sync_price(t):
    """The fake testnet trades at the same price the fake mainnet reports (today's open)."""
    t.broker.last_price = t.market.price(t.symbol)


def run_days(t, days, hours=(0, 13)):
    for day in days:
        for h in hours:
            t.clock.t = day + pd.Timedelta(hours=h, minutes=5)
            sync_price(t)
            t.tick()


# ---------- the key test: live loop == backtest ----------

def test_live_loop_matches_backtest(tmp_path, candles):
    """Replay 1.5 years through the live loop (fake exchange, same costs). Every trade must match
    the backtest engine's: same days, same side, same size. Extra hourly ticks must not trade."""
    df = candles["BTC/USDT"]
    start = pd.Timestamp("2019-01-01", tz="UTC")
    t = make_trader(tmp_path, candles, start)
    run_days(t, df.index[df.index >= start])

    window = df[df.index >= start - pd.Timedelta(days=1)]
    target = VolTargetTrend().target_positions(df).loc[window.index]
    bt = run_backtest(window, target, CostModel(), 1_000.0)
    live = t.state.orders(status="filled")

    assert len(bt.trades) > 10
    assert [o["trade_date"] for o in live] == [ts.date().isoformat() for ts in bt.trades["time"]]
    assert [o["side"] for o in live] == bt.trades["side"].tolist()
    signed = np.where(bt.trades["side"] == "buy", 1, -1) * bt.trades["units"]
    # Sizes match up to the exchange's lot step (1e-8 coin), which the engine doesn't round to.
    np.testing.assert_allclose([o["base_delta"] for o in live], signed, rtol=1e-6, atol=2e-8)

    # Same final equity once valued at the last close, like the engine does.
    units, cash = t.account()
    assert cash + units * df["close"].iloc[-1] == pytest.approx(bt.equity.iloc[-1], rel=1e-6)


def test_one_decision_per_day(tmp_path, candles):
    df = candles["BTC/USDT"]
    start = pd.Timestamp("2019-03-01", tz="UTC")
    t = make_trader(tmp_path, candles, start)
    run_days(t, df.index[(df.index >= start) & (df.index < start + pd.Timedelta(days=30))], hours=range(0, 24, 3))
    dates = [d["trade_date"] for d in t.state.decisions()]
    assert len(dates) == len(set(dates)) == 30


def test_targets_from_recent_window_equal_full_history(candles):
    """Live fetches ~400 candles; the backtest used all of them. The last target must agree."""
    df = candles["BTC/USDT"]
    for strat in (VolTargetTrend(), VolTargetBlend()):
        assert strat.target_positions(df.tail(400)).iloc[-1] == pytest.approx(strat.target_positions(df).iloc[-1])


def test_shadow_ledgers_reported(tmp_path, candles):
    df = candles["BTC/USDT"]
    start = pd.Timestamp("2019-01-01", tz="UTC")
    t = make_trader(tmp_path, candles, start)
    run_days(t, df.index[(df.index >= start) & (df.index < start + pd.Timedelta(days=60))], hours=(0,))
    report = t.alerts.messages[-1]
    for name in ("backtest of vol_target", "shadow A (blend)", "shadow B (50/50 BTC+ETH)", "buy & hold BTC"):
        assert name in report

    # The live ledger, valued at yesterday's close before today's trade, equals the backtest ledger.
    today = t.clock().floor("D")
    yclose = df.loc[today - pd.Timedelta(days=1), "close"]
    ledger = t.state.get("ledger")
    today_fill = [o for o in t.state.orders(status="filled") if o["trade_date"] == today.date().isoformat()]
    units = ledger["units"] - sum(o["base_delta"] for o in today_fill)
    cash = ledger["cash"] - sum(o["quote_delta"] for o in today_fill)
    shadows = t.shadows(t.clock())
    assert cash + units * yclose == pytest.approx(shadows["backtest of vol_target(100,30,0.5)"], rel=1e-6)


# ---------- crash safety ----------

def first_trade_day(t, df, start):
    """Move the clock to the first day the strategy wants a position. Returns without ticking."""
    for day in df.index[df.index >= start]:
        t.clock.t = day + pd.Timedelta(minutes=5)
        sync_price(t)
        target = VolTargetTrend().target_positions(t.market.daily_candles(t.symbol, t.clock.t)).iloc[-1]
        if target > 0.1:
            return day
    raise AssertionError("no buy signal")


def test_crash_after_send_does_not_trade_twice(tmp_path, candles):
    df = candles["BTC/USDT"]
    t = make_trader(tmp_path, candles, "2019-01-01")
    first_trade_day(t, df, t.clock.t)
    t.broker.fail_after_send = True
    with pytest.raises(ConnectionError):
        t.tick()
    assert t.state.orders(status="pending")
    t.clock.t += pd.Timedelta(hours=1)
    t.tick()  # resolves the pending order as filled, does not send another
    assert len(t.broker.orders) == 1
    assert len(t.state.orders(status="filled")) == 1
    assert t.state.get("last_trade_date") == t.clock.t.date().isoformat()
    assert any("Recovered" in m for m in t.alerts.messages)


def test_crash_before_send_retries_once(tmp_path, candles):
    df = candles["BTC/USDT"]
    t = make_trader(tmp_path, candles, "2019-01-01")
    first_trade_day(t, df, t.clock.t)
    t.broker.fail_before_send = True
    with pytest.raises(ConnectionError):
        t.tick()
    t.clock.t += pd.Timedelta(hours=1)
    t.tick()
    assert [o["status"] for o in t.state.orders()] == ["not_placed", "filled"]
    assert len(t.broker.orders) == 1


def test_too_many_attempts_halts(tmp_path, candles):
    df = candles["BTC/USDT"]
    t = make_trader(tmp_path, candles, "2019-01-01")
    first_trade_day(t, df, t.clock.t)
    for _ in range(2):
        t.broker.fail_before_send = True
        with pytest.raises(ConnectionError):
            t.tick()
    t.tick()
    assert "orders already tried" in t.state.get("halted")["reason"]
    assert not t.broker.orders


# ---------- risk limits ----------

def holding_trader(tmp_path, candles, limits=Limits()):
    """A trader that has just bought."""
    df = candles["BTC/USDT"]
    t = make_trader(tmp_path, candles, "2019-01-01", limits=limits)
    first_trade_day(t, df, t.clock.t)
    t.tick()
    assert t.account()[0] > 0
    return t


def set_price(t, price):
    t.market.price = lambda s: price
    t.broker.last_price = price


def test_hard_daily_loss_halts_and_holds(tmp_path, candles):
    t = holding_trader(tmp_path, candles)
    units_before = t.account()[0]
    t.clock.t += pd.Timedelta(hours=5)
    set_price(t, 0.5 * t.broker.last_price)  # crash 50% intraday
    t.tick()
    assert "down" in t.state.get("halted")["reason"]
    assert t.account()[0] == units_before  # position kept, nothing sold
    n = len(t.broker.orders)
    t.clock.t += pd.Timedelta(days=1)
    t.tick()  # stays halted the next day
    assert len(t.broker.orders) == n


def test_soft_daily_loss_alerts_once(tmp_path, candles):
    t = holding_trader(tmp_path, candles)
    pos = t.account()[0] * t.broker.last_price / 1_000.0
    set_price(t, t.broker.last_price * (1 - 0.07 / pos))  # equity down about 7%
    for h in (3, 4, 5):
        t.clock.t = t.clock.t.floor("D") + pd.Timedelta(hours=h)
        t.tick()
    assert sum("Warning: down" in m for m in t.alerts.messages) == 1
    assert t.state.get("halted") is None


def test_drawdown_halt(tmp_path, candles):
    t = holding_trader(tmp_path, candles, Limits(hard_daily_loss=-1, max_drawdown=-0.05))
    t.state.set("peak_equity", 2_000.0)
    t.clock.t += pd.Timedelta(hours=1)
    t.tick()
    assert "drawdown" in t.state.get("halted")["reason"]


def test_reconciliation_mismatch_halts(tmp_path, candles):
    t = holding_trader(tmp_path, candles)
    t.broker.bal["BTC"] -= 0.5 * t.account()[0]  # coins vanish (e.g. testnet reset)
    t.clock.t += pd.Timedelta(hours=1)
    t.tick()
    assert "differ" in t.state.get("halted")["reason"]


def test_resume_clears_halt(tmp_path, candles):
    t = holding_trader(tmp_path, candles)
    t.halt("test")
    t.resume()
    assert t.state.get("halted") is None


def test_price_gap_skips_trade(tmp_path, candles):
    df = candles["BTC/USDT"]
    t = make_trader(tmp_path, candles, "2019-01-01")
    first_trade_day(t, df, t.clock.t)
    t.broker.last_price *= 1.10
    for h in range(3):
        t.clock.t = t.clock.t.floor("D") + pd.Timedelta(hours=h, minutes=5)
        t.tick()
    assert not t.broker.orders
    assert sum("away from the real market" in m for m in t.alerts.messages) == 1
    assert t.state.get("last_trade_date") is None  # will retry


def test_stale_candles_skip_trade(tmp_path, candles):
    t = make_trader(tmp_path, candles, "2019-01-01")
    t.clock.t = pd.Timestamp("2021-01-01 00:05", tz="UTC")  # synthetic data ends 2020-06-01
    set_price(t, 100.0)
    t.tick()
    assert not t.broker.orders
    assert any("stale" in m for m in t.alerts.messages)


def test_below_exchange_minimum_holds(tmp_path, candles):
    df = candles["BTC/USDT"]
    t = make_trader(tmp_path, candles, "2019-01-01", min_notional=10_000.0)
    first_trade_day(t, df, t.clock.t)
    t.tick()
    assert not t.broker.orders
    assert "below exchange minimum" in t.state.decisions()[-1]["reason"]


# ---------- kill switch ----------

def test_kill_file_sells_everything_and_stays_stopped(tmp_path, candles):
    t = holding_trader(tmp_path, candles)
    t.s.kill_path.touch()
    t.clock.t += pd.Timedelta(hours=1)
    t.tick()
    assert t.account()[0] == pytest.approx(0, abs=1e-8)
    assert t.state.get("halted")["reason"] == "killed"
    n = len(t.broker.orders)
    t.clock.t += pd.Timedelta(days=3)
    t.tick()
    assert len(t.broker.orders) == n  # sold once, then nothing
    with pytest.raises(RuntimeError, match="Kill file"):
        t.resume()
    t.s.kill_path.unlink()
    t.resume()
    assert t.state.get("halted") is None


# ---------- pure pieces ----------

def test_plan_order_mirrors_engine_rules():
    c = CostModel()
    assert plan_order(0.5, 0, 1000, 100, c) == OrderPlan("buy", 500)
    assert plan_order(0.01, 0, 1000, 100, c) is None  # under min_rebalance
    assert plan_order(1.0, 0, 1000, 100, c).amount == pytest.approx(1000 / 1.001)  # fee-aware cap
    assert plan_order(0.0, 0.001, 1000, 100, c) == OrderPlan("sell", 0.001)  # full exit, even tiny
    assert plan_order(0.25, 5, 500, 100, c).amount == pytest.approx(2.5)


def test_check_order_rejects_bad_sizes():
    assert check_order(OrderPlan("buy", 2000), 0, 1000, 100)
    assert check_order(OrderPlan("sell", 2), 1, 0, 100)
    assert check_order(OrderPlan("buy", -1), 0, 1000, 100)
    assert check_order(OrderPlan("buy", 500), 0, 1000, 100) is None


def test_refuses_non_testnet():
    import ccxt

    with pytest.raises(RuntimeError, match="Refusing"):
        assert_testnet(ccxt.binance())
    ex = ccxt.binance()
    ex.set_sandbox_mode(True)
    assert_testnet(ex)
    ex.urls["api"]["private"] = "https://api.binance.com/api/v3"
    with pytest.raises(RuntimeError, match="Refusing"):
        assert_testnet(ex)


def test_fill_from_ccxt_fees():
    buy = {"side": "buy", "filled": 0.01, "cost": 1000.0, "average": 100_000.0,
           "fees": [{"currency": "BTC", "cost": 0.00001}]}
    f = fill_from_ccxt(buy, "BTC", "USDT")
    assert (f.base_delta, f.quote_delta, f.fee_quote) == (pytest.approx(0.00999), -1000.0, pytest.approx(1.0))
    sell = {"side": "sell", "filled": 0.01, "cost": 1000.0, "average": 100_000.0,
            "fees": [{"currency": "USDT", "cost": 1.0}]}
    assert fill_from_ccxt(sell, "BTC", "USDT") == Fill(-0.01, -1000.0 * -1 - 1.0, 100_000.0, 1.0)
    assert fill_from_ccxt({"side": "buy", "filled": 0}, "BTC", "USDT") is None


def test_settings_hide_secrets(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nBINANCE_TESTNET_API_KEY='abc123'\nALLOCATED_USDT=500\nHARD_DAILY_LOSS=-0.001\n")
    assert read_dotenv(env)["BINANCE_TESTNET_API_KEY"] == "abc123"
    s = Settings.load(env={}, dotenv=env)
    assert s.api_key == "abc123" and s.allocated_usdt == 500
    assert Limits(**s.limit_overrides).hard_daily_loss == -0.001
    assert "abc123" not in repr(s)


# ---------- exchange outages ----------

def flaky(failures, error):
    calls = []

    def call():
        calls.append(1)
        if len(calls) <= failures:
            raise error
        return "ok"
    return call, calls


def test_retry_recovers_from_brief_outage():
    import ccxt
    call, calls = flaky(2, ccxt.ExchangeNotAvailable("500"))
    waits = []
    assert retry(call, sleep=waits.append) == "ok"
    assert len(calls) == 3 and len(waits) == 2


def test_retry_gives_up_after_last_attempt():
    import ccxt
    call, calls = flaky(99, ccxt.RequestTimeout("slow"))
    with pytest.raises(ccxt.RequestTimeout):
        retry(call, sleep=lambda _: None)
    assert len(calls) == 3


def test_retry_does_not_retry_other_errors():
    import ccxt
    call, calls = flaky(1, ccxt.InsufficientFunds("no"))
    with pytest.raises(ccxt.InsufficientFunds):
        retry(call, sleep=lambda _: None)
    assert len(calls) == 1


def test_outage_alerts_only_when_it_lasts(tmp_path, candles):
    t = make_trader(tmp_path, candles, "2019-06-01")
    t.alerts.messages.clear()
    alerts = [t.outage("ExchangeNotAvailable") for _ in range(OUTAGE_ALERT_AFTER + OUTAGE_REALERT_EVERY)]
    assert [i + 1 for i, a in enumerate(alerts) if a] == [OUTAGE_ALERT_AFTER, OUTAGE_ALERT_AFTER + OUTAGE_REALERT_EVERY]
    t.outage_over()
    assert len(t.alerts.messages) == 1 and "reachable again" in t.alerts.messages[0]
    assert t.state.get("outage_runs") is None


def test_brief_outage_recovers_silently(tmp_path, candles):
    t = make_trader(tmp_path, candles, "2019-06-01")
    t.alerts.messages.clear()
    for _ in range(OUTAGE_ALERT_AFTER - 1):
        assert not t.outage("ExchangeNotAvailable")
    t.outage_over()
    assert t.alerts.messages == [] and t.state.get("outage_runs") is None
    assert not t.outage("ExchangeNotAvailable")  # the count starts over
