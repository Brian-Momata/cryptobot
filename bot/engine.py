"""Backtest simulator.

How a candle is processed:
  1. The strategy decides a target position at the CLOSE of candle t.
  2. The order executes at the OPEN of candle t+1 (you can't trade at a price you've
     already seen close), with slippage against you and an exchange fee.
  3. Equity is marked at each candle's close.

Spot only: position is 0..100% of equity, cash never goes negative.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class CostModel:
    fee: float = 0.001  # 0.1% per trade (Binance spot taker fee without discounts)
    slippage: float = 0.0005  # 0.05% worse than the open price on every fill
    min_rebalance: float = 0.02  # skip trades smaller than 2% of equity (saves fees on tiny tweaks)


@dataclass
class BacktestResult:
    equity: pd.Series  # portfolio value at each close
    position: pd.Series  # fraction of equity in the coin at each close
    trades: pd.DataFrame
    target: pd.Series  # what the strategy asked for
    initial_cash: float
    fees_paid: float
    label: str = ""
    extra: dict = field(default_factory=dict)


def run_backtest(
    df: pd.DataFrame,
    target: pd.Series,
    costs: CostModel | None = None,
    initial_cash: float = 1_000.0,
    label: str = "",
) -> BacktestResult:
    costs = costs or CostModel()
    target = target.reindex(df.index).astype(float)
    if not target.dropna().between(0, 1).all():
        raise ValueError("target positions must be between 0 and 1 (spot, no leverage)")
    tgt = target.fillna(0.0).clip(0, 1).to_numpy()

    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)

    cash = initial_cash
    units = 0.0
    fees_paid = 0.0
    equity = np.empty(n)
    position = np.empty(n)
    trades = []

    for i in range(n):
        if i > 0:
            desired = tgt[i - 1]  # decided at previous close, executed now at this open
            price = opens[i]
            eq_open = cash + units * price
            current_frac = units * price / eq_open if eq_open > 0 else 0.0
            diff = desired - current_frac
            full_exit = desired == 0.0 and units > 0
            full_entry_or_change = abs(diff) >= costs.min_rebalance

            if eq_open > 0 and (full_exit or full_entry_or_change):
                if diff > 0:  # buy
                    notional = min(diff * eq_open, cash / (1 + costs.fee))
                    if notional > 0:
                        fill = price * (1 + costs.slippage)
                        qty = notional / fill
                        fee = notional * costs.fee
                        cash -= notional + fee
                        units += qty
                        fees_paid += fee
                        trades.append((df.index[i], "buy", fill, qty, notional, fee))
                else:  # sell
                    qty = units if desired == 0.0 else min(units, -diff * eq_open / price)
                    if qty > 0:
                        fill = price * (1 - costs.slippage)
                        proceeds = qty * fill
                        fee = proceeds * costs.fee
                        cash += proceeds - fee
                        units -= qty
                        fees_paid += fee
                        trades.append((df.index[i], "sell", fill, qty, proceeds, fee))
            cash = max(cash, 0.0)  # guard tiny negative float error

        equity[i] = cash + units * closes[i]
        position[i] = units * closes[i] / equity[i] if equity[i] > 0 else 0.0

    trades_df = pd.DataFrame(trades, columns=["time", "side", "price", "units", "notional", "fee"])
    return BacktestResult(
        equity=pd.Series(equity, index=df.index, name="equity"),
        position=pd.Series(position, index=df.index, name="position"),
        trades=trades_df,
        target=target,
        initial_cash=initial_cash,
        fees_paid=fees_paid,
        label=label,
    )
