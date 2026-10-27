"""Exchange access.

Two separate connections, on purpose:
  * MainnetData: real Binance, PUBLIC endpoints only (candles, prices). Created without keys.
    Signals come from here so they match the backtest's data exactly.
  * TestnetBroker: Binance spot TESTNET, with testnet keys. Orders go here. It refuses to start
    unless every private URL points at the testnet, so live trading needs a code change.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pandas as pd

from ..data import _rows_to_df

TESTNET_HOST = "testnet.binance.vision"
DAY_MS = 86_400_000
RETRY_WAITS = (5, 30)  # seconds between attempts: 3 tries over ~35s, well inside the 15 min task limit


def retry(call, waits=RETRY_WAITS, sleep=time.sleep):
    """Run `call()`, retrying when the exchange is briefly unreachable (timeouts, 5xx, rate limits).
    Only for READ calls: an order that errors may still have arrived, so orders are never retried
    here (the pending-order check in the trader handles those)."""
    import ccxt

    for wait in waits:
        try:
            return call()
        except ccxt.NetworkError:
            sleep(wait)
    return call()


@dataclass
class Fill:
    """What an order did to the account, signed: buys have base_delta > 0, quote_delta < 0."""

    base_delta: float
    quote_delta: float
    avg_price: float
    fee_quote: float  # all fees converted to the quote currency, for reporting


def fill_from_ccxt(order: dict, base: str, quote: str) -> Fill | None:
    """Convert a ccxt order to a Fill. None if nothing was filled."""
    filled = float(order.get("filled") or 0)
    if filled <= 0:
        return None
    cost = float(order.get("cost") or 0)
    avg = float(order.get("average") or cost / filled)
    fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
    fee_base = sum(float(f.get("cost") or 0) for f in fees if f and f.get("currency") == base)
    fee_quote = sum(float(f.get("cost") or 0) for f in fees if f and f.get("currency") == quote)
    sign = 1 if order["side"] == "buy" else -1
    # Fees in another currency (e.g. BNB) don't touch base or quote, so they're left out here.
    return Fill(sign * filled - fee_base, -sign * cost - fee_quote, avg, fee_quote + fee_base * avg)


def assert_testnet(ex) -> None:
    """Hard stop unless the exchange object can only reach the testnet. Deliberately not an
    `assert` statement (those vanish under python -O) and not a config flag."""
    urls = ex.urls["api"]
    bad = {k: v for k, v in urls.items() if isinstance(v, str) and k in ("public", "private")
           and TESTNET_HOST not in v}
    if not getattr(ex, "isSandboxModeEnabled", False) or bad or not urls.get("private"):
        raise RuntimeError(f"Refusing to start: not pointed at the Binance spot testnet ({bad or 'sandbox off'})")


class TestnetBroker:
    __test__ = False  # not a pytest test class

    def __init__(self, api_key: str, api_secret: str, symbol: str):
        import ccxt

        if not api_key or not api_secret:
            raise RuntimeError("Testnet API key/secret missing: set them in .env (see docs/deploy.md)")
        # Spot markets only: by default ccxt also loads futures, so a futures-testnet outage
        # would stop a bot that never trades futures.
        ex = ccxt.binance({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True,
                           "options": {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}}})
        ex.set_sandbox_mode(True)
        assert_testnet(ex)
        self._ex, self.ccxt = ex, ccxt
        self.base, self.quote = symbol.split("/")

    @property
    def ex(self):
        """The exchange, with markets loaded on first use rather than in __init__, so a network
        error while connecting happens inside the tick and counts as an outage like any other."""
        if not self._ex.markets:
            retry(self._ex.load_markets)
        return self._ex

    def balances(self) -> dict[str, float]:
        return {k: float(v or 0) for k, v in retry(self.ex.fetch_balance)["total"].items()}

    def price(self, symbol: str) -> float:
        return float(retry(lambda: self.ex.fetch_ticker(symbol))["last"])

    def market_buy(self, symbol: str, cost: float, client_id: str) -> Fill:
        order = self.ex.create_market_buy_order_with_cost(symbol, cost, {"newClientOrderId": client_id})
        return self._fill(order)

    def market_sell(self, symbol: str, qty: float, client_id: str) -> Fill:
        order = self.ex.create_order(symbol, "market", "sell", qty, None, {"newClientOrderId": client_id})
        return self._fill(order)

    def _fill(self, order: dict) -> Fill:
        if order.get("filled") is None or order.get("fees") is None:  # thin response: ask again
            order = retry(lambda: self.ex.fetch_order(order["id"], order["symbol"]))
        fill = fill_from_ccxt(order, self.base, self.quote)
        if fill is None:
            raise RuntimeError(f"Order {order.get('clientOrderId')} was not filled (status {order.get('status')})")
        return fill

    def find_order(self, symbol: str, client_id: str) -> Fill | None:
        try:
            order = retry(lambda: self.ex.fetch_order("", symbol, {"origClientOrderId": client_id}))
        except self.ccxt.OrderNotFound:
            return None
        return fill_from_ccxt(order, self.base, self.quote)

    def cancel_all(self, symbol: str) -> int:
        open_orders = retry(lambda: self.ex.fetch_open_orders(symbol))
        for o in open_orders:
            self.ex.cancel_order(o["id"], symbol)
        return len(open_orders)

    def round_amount(self, symbol: str, qty: float) -> float:
        try:
            return float(self.ex.amount_to_precision(symbol, qty))
        except self.ccxt.BaseError:  # below the smallest tradable step
            return 0.0

    def round_cost(self, symbol: str, cost: float) -> float:
        try:
            return float(self.ex.cost_to_precision(symbol, cost))
        except self.ccxt.BaseError:
            return 0.0

    def min_notional(self, symbol: str) -> float:
        return float(self.ex.market(symbol)["limits"]["cost"].get("min") or 5.0)


class MainnetData:
    """Real-market candles and prices from Binance's public API. Never given API keys."""

    def __init__(self):
        import ccxt

        self.ex = ccxt.binance({"enableRateLimit": True})

    def daily_candles(self, symbol: str, now: pd.Timestamp, days: int = 450) -> pd.DataFrame:
        """Completed daily candles for the last `days` days (today's forming candle excluded)."""
        today = now.floor("D")
        since = int((today - pd.Timedelta(days=days)).timestamp() * 1000)
        rows: list[list] = []
        while since < int(today.timestamp() * 1000):
            batch = retry(lambda: self.ex.fetch_ohlcv(symbol, "1d", since=since, limit=1000))
            if not batch:
                break
            rows.extend(batch)
            since = batch[-1][0] + DAY_MS
        df = _rows_to_df(rows)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df[df.index < today]

    def today_open(self, symbol: str, now: pd.Timestamp) -> float | None:
        """Open of today's (still forming) daily candle: the price the backtest would fill at."""
        since = int(now.floor("D").timestamp() * 1000)
        batch = retry(lambda: self.ex.fetch_ohlcv(symbol, "1d", since=since, limit=1))
        return float(batch[0][1]) if batch else None

    def price(self, symbol: str) -> float:
        return float(retry(lambda: self.ex.fetch_ticker(symbol))["last"])
