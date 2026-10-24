"""Tests the download/caching logic against a fake exchange (no internet needed)."""
import sys
import types

import pandas as pd

from bot import data

DAY = 86_400_000
START = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)


class FakeExchange:
    rateLimit = 0

    def __init__(self, cfg):
        self.calls = 0

    def milliseconds(self):
        return START + 2500 * DAY + DAY // 2  # mid-way through a still-forming candle

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        self.calls += 1
        end = self.milliseconds()
        rows = []
        t = since
        while t <= end and len(rows) < limit:
            rows.append([t, 1.0, 2.0, 0.5, 1.5, 10.0])
            t += DAY
        return rows


def test_fetch_paginates_caches_and_drops_forming_candle(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "DATA_DIR", tmp_path)
    monkeypatch.setitem(sys.modules, "ccxt", types.SimpleNamespace(binance=FakeExchange))

    df = data.fetch_ohlcv("BTC/USDT", "1d", since="2024-01-01")
    assert len(df) == 2500  # 2501 candles exist, the last is still forming and dropped
    assert df.index.is_monotonic_increasing and not df.index.duplicated().any()

    # Second run only adds what's new and keeps the cache consistent.
    df2 = data.fetch_ohlcv("BTC/USDT", "1d", since="2024-01-01")
    assert len(df2) == 2500
    pd.testing.assert_frame_equal(df, data.load_data("BTC/USDT", "1d"), check_freq=False)
