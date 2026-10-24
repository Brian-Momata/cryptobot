"""Performance metrics. All computed from the equity curve, so fees and slippage are included."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import BacktestResult


def max_drawdown(equity: pd.Series) -> float:
    """Worst peak-to-trough fall, as a negative fraction (e.g. -0.55 = fell 55% from a high)."""
    peak = equity.cummax()
    return float((equity / peak - 1).min())


def drawdown_series(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1


def round_trips(result: BacktestResult) -> pd.DataFrame:
    """Each stretch from flat -> in a position -> back to flat, with its return (fees included)."""
    pos = result.position.to_numpy()
    eq = result.equity.to_numpy()
    idx = result.equity.index
    rows = []
    start = None
    for i in range(1, len(pos)):
        in_now, in_before = pos[i] > 1e-9, pos[i - 1] > 1e-9
        if in_now and not in_before:
            start = i - 1  # equity at the close before entry
        elif in_before and not in_now and start is not None:
            rows.append((idx[start + 1], idx[i], eq[i] / eq[start] - 1))
            start = None
    if start is not None:  # still open at the end
        rows.append((idx[start + 1], idx[-1], eq[-1] / eq[start] - 1))
    return pd.DataFrame(rows, columns=["entry", "exit", "return"])


def summarize(result: BacktestResult, bars_per_year: float) -> dict:
    eq = result.equity
    rets = eq.pct_change().dropna()
    years = len(eq) / bars_per_year
    total = eq.iloc[-1] / result.initial_cash - 1
    cagr = (eq.iloc[-1] / result.initial_cash) ** (1 / years) - 1 if years > 0 and eq.iloc[-1] > 0 else np.nan
    std = rets.std()
    sharpe = rets.mean() / std * np.sqrt(bars_per_year) if std > 0 else np.nan
    downside = rets[rets < 0].std()
    sortino = rets.mean() / downside * np.sqrt(bars_per_year) if downside and downside > 0 else np.nan
    mdd = max_drawdown(eq)
    rt = round_trips(result)
    return {
        "strategy": result.label,
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": mdd,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": cagr / abs(mdd) if mdd < 0 else np.nan,
        "trades": len(result.trades),
        "round_trips": len(rt),
        "win_rate": float((rt["return"] > 0).mean()) if len(rt) else np.nan,
        "avg_trade": float(rt["return"].mean()) if len(rt) else np.nan,
        "time_in_market": float((result.position > 1e-9).mean()),
        "fees_paid": result.fees_paid,
        "final_equity": float(eq.iloc[-1]),
    }


def yearly_returns(equity: pd.Series, initial: float) -> pd.Series:
    """Return for each calendar year (a partial first/last year covers only the data present)."""
    rets = equity.pct_change()
    rets.iloc[0] = equity.iloc[0] / initial - 1
    return (1 + rets).groupby(equity.index.year).prod() - 1


PCT_COLS = ["total_return", "cagr", "max_drawdown", "win_rate", "avg_trade", "time_in_market"]


def format_table(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows).set_index("strategy")
    out = pd.DataFrame(index=df.index)
    for c in df.columns:
        if c in PCT_COLS:
            out[c] = df[c].map(lambda v: "" if pd.isna(v) else f"{v * 100:,.1f}%")
        elif c in ("sharpe", "sortino", "calmar"):
            out[c] = df[c].map(lambda v: "" if pd.isna(v) else f"{v:.2f}")
        elif c in ("fees_paid", "final_equity"):
            out[c] = df[c].map(lambda v: f"${v:,.0f}")
        else:
            out[c] = df[c]
    return out
