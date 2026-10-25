"""In-memory exchange, market data and alerts, so the whole live loop runs in pytest offline.

The fake exchange charges fees and slippage exactly like bot/engine.py, which is what makes
the live/backtest parity test meaningful.
"""
from __future__ import annotations

import math

import pandas as pd

from .broker import Fill


class FakeBroker:
    def __init__(self, balances: dict[str, float], price: float = 100.0, fee: float = 0.001,
                 slippage: float = 0.0005, min_notional: float = 5.0, step: float = 1e-8, symbol: str = "BTC/USDT"):
        self.bal = dict(balances)
        self.last_price = price
        self.fee, self.slippage, self.min_not, self.step = fee, slippage, min_notional, step
        self.base, self.quote = symbol.split("/")
        self.orders: dict[str, Fill] = {}
        self.fail_after_send = False  # simulate a crash after the exchange accepted the order
        self.fail_before_send = False  # simulate a network error before it arrived
        self.cancelled = 0

    def balances(self) -> dict[str, float]:
        return dict(self.bal)

    def price(self, symbol: str) -> float:
        return self.last_price

    def _place(self, client_id: str, fill: Fill) -> Fill:
        if self.fail_before_send:
            self.fail_before_send = False
            raise ConnectionError("simulated network error before send")
        if client_id in self.orders:
            raise ValueError(f"duplicate clientOrderId {client_id}")
        self.bal[self.base] = self.bal.get(self.base, 0.0) + fill.base_delta
        self.bal[self.quote] = self.bal.get(self.quote, 0.0) + fill.quote_delta
        self.orders[client_id] = fill
        if self.fail_after_send:
            self.fail_after_send = False
            raise ConnectionError("simulated crash after send")
        return fill

    def market_buy(self, symbol: str, cost: float, client_id: str) -> Fill:
        px = self.last_price * (1 + self.slippage)
        fee = cost * self.fee
        if cost + fee > self.bal.get(self.quote, 0.0) + 1e-9:
            raise ValueError("insufficient balance")
        return self._place(client_id, Fill(cost / px, -(cost + fee), px, fee))

    def market_sell(self, symbol: str, qty: float, client_id: str) -> Fill:
        if qty > self.bal.get(self.base, 0.0) + 1e-12:
            raise ValueError("insufficient balance")
        px = self.last_price * (1 - self.slippage)
        proceeds = qty * px
        return self._place(client_id, Fill(-qty, proceeds * (1 - self.fee), px, proceeds * self.fee))

    def find_order(self, symbol: str, client_id: str) -> Fill | None:
        return self.orders.get(client_id)

    def cancel_all(self, symbol: str) -> int:
        self.cancelled += 1
        return 0

    def round_amount(self, symbol: str, qty: float) -> float:
        return math.floor(qty / self.step + 1e-9) * self.step

    def round_cost(self, symbol: str, cost: float) -> float:
        return math.floor(cost / self.step + 1e-9) * self.step

    def min_notional(self, symbol: str) -> float:
        return self.min_not


class FakeMarket:
    """Serves candles as if `clock()` were now: only candles completed before today, and today's
    open as the current price (the price the backtest fills at)."""

    def __init__(self, candles: dict[str, pd.DataFrame], clock):
        self.candles, self.clock = candles, clock

    def daily_candles(self, symbol: str, now: pd.Timestamp, days: int = 450) -> pd.DataFrame:
        df = self.candles[symbol]
        today = now.floor("D")
        return df[(df.index < today) & (df.index >= today - pd.Timedelta(days=days))]

    def today_open(self, symbol: str, now: pd.Timestamp) -> float | None:
        df = self.candles[symbol]
        today = now.floor("D")
        return float(df.loc[today, "open"]) if today in df.index else None

    def price(self, symbol: str) -> float:
        return self.today_open(symbol, self.clock())


class RecordingAlerts:
    def __init__(self):
        self.messages: list[str] = []
        self.heartbeats = 0
        self.failed_heartbeats = 0

    def send(self, text: str) -> None:
        self.messages.append(text)

    def heartbeat(self, failed: bool = False) -> None:
        if failed:
            self.failed_heartbeats += 1
        else:
            self.heartbeats += 1
