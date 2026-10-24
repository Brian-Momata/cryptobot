"""Strategies.

Contract (the same in backtest, paper and live trading):
    target = strategy.target_positions(candles)

`candles` is a DataFrame of completed candles (open, high, low, close, volume).
The result is a Series aligned to it: the fraction of equity to hold in the coin
(0 = all cash, 1 = fully in) decided at the CLOSE of each candle.

Rules every strategy must follow:
  * Only causal calculations (rolling windows, ewm, loops that look backwards).
    The value at candle t may use data up to and including candle t, never after.
    tests/test_no_lookahead.py checks this automatically for every registered strategy.
  * Values must be between 0 and 1 (spot, no leverage, no shorting).

In live trading the bot just calls this on the latest candles and uses the last value.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


class Strategy:
    name = "base"
    params: dict = {}

    def __init__(self, **params):
        unknown = set(params) - set(self.params)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}; valid: {sorted(self.params)}")
        self.p = {**self.params, **params}

    def target_positions(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    def label(self) -> str:
        if not self.p:
            return self.name
        shown = {k: v for k, v in self.p.items() if k != "bars_per_year"}
        return f"{self.name}(" + ",".join(f"{v:g}" if isinstance(v, float) else str(v) for v in shown.values()) + ")"


class BuyAndHold(Strategy):
    """Benchmark: buy on day one and never sell. Every strategy has to beat this."""

    name = "buy_hold"
    params = {}

    def target_positions(self, df):
        return pd.Series(1.0, index=df.index)


class FixedFraction(Strategy):
    """Benchmark: always hold `fraction` of equity in the coin, the rest in cash.
    Holding less always shrinks drawdowns, so a strategy that is often partly in cash
    should be compared against this at the same average exposure, not only buy & hold."""

    name = "fixed_fraction"
    params = {"fraction": 0.5}

    def target_positions(self, df):
        return pd.Series(float(self.p["fraction"]), index=df.index)


class SMACross(Strategy):
    """Trend following: hold while the fast moving average is above the slow one."""

    name = "sma_cross"
    params = {"fast": 50, "slow": 200}

    def target_positions(self, df):
        fast = df["close"].rolling(int(self.p["fast"])).mean()
        slow = df["close"].rolling(int(self.p["slow"])).mean()
        return (fast > slow).astype(float)


class Momentum(Strategy):
    """Time-series momentum: hold if price is higher than it was `lookback` candles ago."""

    name = "momentum"
    params = {"lookback": 90}

    def target_positions(self, df):
        past = df["close"].shift(int(self.p["lookback"]))
        return (df["close"] > past).astype(float)


class DonchianBreakout(Strategy):
    """Breakout (Turtle-style): buy when price closes above the highest high of the last
    `entry` candles, sell when it closes below the lowest low of the last `exit` candles."""

    name = "donchian"
    params = {"entry": 20, "exit": 10}

    def target_positions(self, df):
        # shift(1): compare against the channel of the PREVIOUS candles, not including this one.
        upper = df["high"].rolling(int(self.p["entry"])).max().shift(1).to_numpy()
        lower = df["low"].rolling(int(self.p["exit"])).min().shift(1).to_numpy()
        close = df["close"].to_numpy()
        pos = np.zeros(len(df))
        holding = 0.0
        for i in range(len(df)):
            if holding == 0.0 and close[i] > upper[i]:
                holding = 1.0
            elif holding == 1.0 and close[i] < lower[i]:
                holding = 0.0
            pos[i] = holding
        return pd.Series(pos, index=df.index)


class RSIReversion(Strategy):
    """Mean reversion: buy when RSI is oversold, sell when it recovers.
    Optional trend filter: only buy dips while price is above its long moving average."""

    name = "rsi_reversion"
    params = {"period": 14, "buy_below": 30, "sell_above": 55, "trend_sma": 200}

    def target_positions(self, df):
        rsi_v = rsi(df["close"], int(self.p["period"])).to_numpy()
        trend_n = int(self.p["trend_sma"])
        if trend_n > 0:
            uptrend = (df["close"] > df["close"].rolling(trend_n).mean()).to_numpy()
        else:
            uptrend = np.ones(len(df), dtype=bool)
        pos = np.zeros(len(df))
        holding = 0.0
        for i in range(len(df)):
            if np.isnan(rsi_v[i]):
                pos[i] = holding
                continue
            if holding == 0.0 and rsi_v[i] < self.p["buy_below"] and uptrend[i]:
                holding = 1.0
            elif holding == 1.0 and rsi_v[i] > self.p["sell_above"]:
                holding = 0.0
            pos[i] = holding
        return pd.Series(pos, index=df.index)


class VolTargetTrend(Strategy):
    """Trend filter + volatility sizing: in an uptrend (price above SMA), hold a position
    sized so the portfolio's expected volatility is about `target_vol` per year.
    Calm market = bigger position, wild market = smaller position."""

    name = "vol_target"
    params = {"sma": 100, "vol_window": 30, "target_vol": 0.5, "bars_per_year": 365}

    def target_positions(self, df):
        close = df["close"]
        uptrend = close > close.rolling(int(self.p["sma"])).mean()
        rets = np.log(close).diff()
        realized = rets.rolling(int(self.p["vol_window"])).std() * np.sqrt(self.p["bars_per_year"])
        size = (self.p["target_vol"] / realized).clip(upper=1.0).fillna(0.0)
        return (size * uptrend).astype(float)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (causal)."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls for cls in [BuyAndHold, FixedFraction, SMACross, Momentum, DonchianBreakout, RSIReversion, VolTargetTrend]
}


def make(name: str, **params) -> Strategy:
    if name not in REGISTRY:
        raise ValueError(f"Unknown strategy '{name}'. Available: {', '.join(REGISTRY)}")
    return REGISTRY[name](**params)
