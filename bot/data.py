"""Price data: download from Binance (via ccxt), cache to CSV, or generate synthetic data for testing."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

TIMEFRAME_MS = {
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}

# Crypto trades 24/7, so a year is 365 full days.
BARS_PER_YEAR = {tf: (365 * 24 * 60 * 60_000) / ms for tf, ms in TIMEFRAME_MS.items()}

COLUMNS = ["open", "high", "low", "close", "volume"]


def cache_path(symbol: str, timeframe: str, exchange: str = "binance") -> Path:
    return DATA_DIR / f"{exchange}_{symbol.replace('/', '-')}_{timeframe}.csv"


def fetch_ohlcv(
    symbol: str = "BTC/USDT",
    timeframe: str = "1d",
    since: str = "2018-01-01",
    exchange: str = "binance",
) -> pd.DataFrame:
    """Download candles from the exchange's public API (no API key needed) and cache them.

    Re-running only downloads candles newer than what's already cached.
    """
    import ccxt  # imported here so the rest of the project works without it

    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"timeframe must be one of {list(TIMEFRAME_MS)}")

    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    path = cache_path(symbol, timeframe, exchange)
    existing = load_csv(path) if path.exists() else None

    start_ms = int(pd.Timestamp(since, tz="UTC").timestamp() * 1000)
    if existing is not None and len(existing):
        start_ms = int(existing.index[-1].timestamp() * 1000) + TIMEFRAME_MS[timeframe]

    rows: list[list] = []
    now_ms = ex.milliseconds()
    while start_ms < now_ms:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=start_ms, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        start_ms = batch[-1][0] + TIMEFRAME_MS[timeframe]
        print(f"  {symbol} {timeframe}: fetched up to {pd.to_datetime(batch[-1][0], unit='ms', utc=True)}")
        time.sleep(ex.rateLimit / 1000)

    new = _rows_to_df(rows)
    df = pd.concat([existing, new]) if existing is not None else new
    df = df[~df.index.duplicated(keep="last")].sort_index()

    # Drop the still-forming candle so we never backtest on incomplete data.
    last_complete = pd.Timestamp(now_ms, unit="ms", tz="UTC") - pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe])
    df = df[df.index <= last_complete]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)
    print(f"Saved {len(df):,} candles to {path}")
    return df


def _rows_to_df(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["timestamp", *COLUMNS])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.as_unit("ns")
    return df.set_index("timestamp").astype(float)


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).as_unit("ns")
    df.index.name = "timestamp"
    return df[COLUMNS].astype(float)


def load_data(symbol: str = "BTC/USDT", timeframe: str = "1d", exchange: str = "binance") -> pd.DataFrame:
    path = cache_path(symbol, timeframe, exchange)
    if not path.exists():
        raise FileNotFoundError(
            f"No cached data at {path}. Run:  python run.py fetch --symbol {symbol} --timeframe {timeframe}"
        )
    return load_csv(path)


def synthetic_data(
    timeframe: str = "1d",
    start: str = "2018-01-01",
    end: str = "2026-10-01",
    seed: int = 42,
    start_price: float = 10_000.0,
) -> pd.DataFrame:
    """Fake but crypto-like prices: alternating bull, bear and sideways regimes with fat-ish tails.

    Only for testing the machinery. Results on this data say nothing about real markets.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, end, freq=pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe]), tz="UTC")
    n = len(idx)
    bars_per_day = 86_400_000 / TIMEFRAME_MS[timeframe]

    # Daily drift / vol per regime, scaled to the bar size.
    regimes = {"bull": (0.0025, 0.035), "bear": (-0.0025, 0.045), "side": (0.0, 0.025)}
    names = list(regimes)
    drift = np.empty(n)
    vol = np.empty(n)
    i = 0
    while i < n:
        name = names[rng.integers(len(names))]
        length = int(rng.integers(90, 400) * bars_per_day)
        mu, sigma = regimes[name]
        drift[i : i + length] = mu / bars_per_day
        vol[i : i + length] = sigma / np.sqrt(bars_per_day)
        i += length

    shocks = rng.standard_t(df=4, size=n) / np.sqrt(2)  # t(4) scaled to ~unit variance
    log_ret = drift + vol * shocks
    close = start_price * np.exp(np.cumsum(log_ret))
    open_ = np.concatenate([[start_price], close[:-1]])
    wiggle = np.abs(rng.normal(0, 0.5, size=n)) * vol
    high = np.maximum(open_, close) * (1 + wiggle)
    low = np.minimum(open_, close) * (1 - wiggle)
    volume = rng.lognormal(10, 0.5, size=n)

    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)
