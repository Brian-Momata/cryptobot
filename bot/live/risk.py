"""Pure functions for order sizing and risk limits. No I/O, so every rule is easy to test."""
from __future__ import annotations

from dataclasses import dataclass

from ..engine import CostModel


@dataclass
class Limits:
    max_orders_per_day: int = 2
    soft_daily_loss: float = -0.05  # alert only
    hard_daily_loss: float = -0.10  # halt and hold (keeps the position)
    max_drawdown: float = -0.40  # halt: outside the strategy's tested behaviour
    max_price_gap: float = 0.03  # testnet vs mainnet price
    max_reconcile_gap: float = 0.01  # exchange vs bot records, as a share of equity
    max_candle_age_hours: float = 26  # since the last completed daily candle closed


@dataclass
class OrderPlan:
    side: str  # "buy" or "sell"
    amount: float  # buy: USDT to spend; sell: coin units to sell


def position_fraction(units: float, cash: float, price: float) -> float:
    equity = cash + units * price
    return units * price / equity if equity > 0 else 0.0


def plan_order(target: float, units: float, cash: float, price: float, costs: CostModel) -> OrderPlan | None:
    """The order the backtest engine would place at this price. Mirrors bot/engine.py line by line,
    so live and backtest size trades identically."""
    equity = cash + units * price
    if equity <= 0:
        return None
    diff = target - units * price / equity
    full_exit = target == 0.0 and units > 0
    if not (full_exit or abs(diff) >= costs.min_rebalance):
        return None
    if diff > 0:
        notional = min(diff * equity, cash / (1 + costs.fee))
        return OrderPlan("buy", notional) if notional > 0 else None
    qty = units if target == 0.0 else min(units, -diff * equity / price)
    return OrderPlan("sell", qty) if qty > 0 else None


def check_order(plan: OrderPlan, units: float, cash: float, price: float) -> str | None:
    """Last line of defence against sizing bugs. Returns a reason if the order must be rejected."""
    equity = cash + units * price
    if plan.amount <= 0:
        return "order amount is not positive"
    if plan.side == "buy":
        if plan.amount > cash + 1e-9:
            return f"buy of {plan.amount:.2f} USDT exceeds available cash {cash:.2f}"
        if (units * price + plan.amount) / equity > 1 + 1e-6:
            return "position would exceed 100% of allocated equity"
    elif plan.side == "sell":
        if plan.amount > units + 1e-12:
            return f"sell of {plan.amount} exceeds held {units} (no shorting)"
    else:
        return f"unknown side {plan.side!r}"
    return None


def change(now: float, ref: float) -> float:
    return now / ref - 1 if ref > 0 else 0.0


def price_gap(a: float, b: float) -> float:
    return abs(a / b - 1) if b > 0 else float("inf")


def reconcile_gap(exchange_units: float, ledger_units: float, exchange_cash: float, ledger_cash: float,
                  price: float, equity: float) -> float:
    """How far the exchange's view differs from the bot's records, as a share of equity."""
    if equity <= 0:
        return float("inf")
    return (abs(exchange_units - ledger_units) * price + abs(exchange_cash - ledger_cash)) / equity
