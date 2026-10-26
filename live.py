"""Paper trading on Binance spot TESTNET. Run hourly by a systemd timer (see docs/deploy.md).

    python live.py --check          test keys, Telegram and data; places no orders
    python live.py --init           start paper trading (records the starting balances)
    python live.py --once           one hourly tick (what the timer runs)
    python live.py --status         show position, limits and recent decisions
    python live.py --kill           sell everything and stop (also: create data/KILL)
    python live.py --resume         clear a halt after you've checked what happened
"""
from __future__ import annotations

import argparse
import sys

import ccxt
import pandas as pd

from bot.live.alerts import Alerts
from bot.live.broker import MainnetData, TestnetBroker
from bot.live.config import Settings
from bot.live.risk import Limits, position_fraction
from bot.live.state import State
from bot.live.trader import Trader


def log(msg: str) -> None:
    print(f"{pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M:%S}Z {msg}", flush=True)


def build(settings: Settings) -> Trader:
    alerts = Alerts(settings.telegram_token, settings.telegram_chat_id, settings.healthcheck_url, log=log)
    broker = TestnetBroker(settings.api_key, settings.api_secret, settings.symbol)
    limits = Limits(**settings.limit_overrides)
    if settings.limit_overrides:
        log(f"Risk limit overrides active: {settings.limit_overrides}")
    return Trader(settings, broker, MainnetData(), State(settings.db_path), alerts, limits=limits, log=log)


def check(settings: Settings) -> None:
    log(f"Settings: {settings!r}")
    t = build(settings)  # raises if not testnet or keys missing
    bal = t.broker.balances()
    log(f"Testnet balances: {t.base} {bal.get(t.base, 0):g}, {t.quote} {bal.get(t.quote, 0):,.2f}")
    log(f"Prices: mainnet {t.market.price(t.symbol):,.2f}, testnet {t.broker.price(t.symbol):,.2f}")
    now = pd.Timestamp.now(tz="UTC")
    candles = t.market.daily_candles(t.symbol, now)
    target = t.strategy.target_positions(candles).iloc[-1]
    log(f"Last completed candle {candles.index[-1]:%Y-%m-%d}; {t.strategy.label()} target today: {target:.0%}")
    if settings.telegram_token and settings.telegram_chat_id:
        t.alerts.send("cryptobot check: Telegram works.")
    else:
        log("Telegram not configured: alerts will only appear in the log.")
    t.alerts.heartbeat()
    log("Check passed. No orders were placed.")


def status(t: Trader) -> None:
    s = t.state
    if not s.get("baseline"):
        print("Not initialised. Run: python live.py --init")
        return
    price = t.market.price(t.symbol)
    units, cash = t.account()
    equity = cash + units * price
    print(f"Started {s.get('started')[:10]}, allocated {t.s.allocated_usdt:,.2f} {t.quote}")
    print(f"Equity {equity:,.2f} {t.quote}: {units:g} {t.base} + {cash:,.2f} {t.quote} "
          f"({position_fraction(units, cash, price):.0%} in {t.base} at {price:,.2f})")
    print(f"Peak {s.get('peak_equity'):,.2f}, last trade date {s.get('last_trade_date')}")
    halted = s.get("halted")
    print(f"HALTED: {halted['reason']} (since {halted['at']})" if halted else "Running (not halted)")
    if t.s.kill_path.exists():
        print(f"Kill file present: {t.s.kill_path}")
    print("\nRecent decisions:")
    for d in s.decisions()[-7:]:
        print(f"  {d['trade_date']}  target {d['target']:.0%}  was {d['current']:.0%}  {d['action']:<5} {d['reason']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    for flag in ("check", "init", "once", "status", "kill", "resume"):
        g.add_argument(f"--{flag}", action="store_true")
    p.add_argument("--force", action="store_true", help="with --init: start over (e.g. after a testnet reset)")
    args = p.parse_args(argv)
    settings = Settings.load()

    if args.check:
        check(settings)
        return 0
    t = build(settings)
    try:
        if args.init:
            t.init(force=args.force)
        elif args.status:
            status(t)
        elif args.kill:
            settings.kill_path.parent.mkdir(parents=True, exist_ok=True)
            settings.kill_path.touch()
            t.kill("--kill command")
        elif args.resume:
            t.resume()
        else:
            t.tick()
            t.outage_over()
    except Exception as e:
        # Report and fail loudly; the next hourly run retries. Keep the message short (no secrets
        # are in it, but exchange errors can be long).
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        if args.once and isinstance(e, ccxt.NetworkError):
            # Exchange still unreachable after the in-run retries: only alert if it keeps happening.
            if not t.outage(msg):
                raise
            msg = f"exchange unreachable for {t.state.get('outage_runs')} hourly runs in a row. Last error: {msg}"
        t.alerts.send(f"ERROR in live.py: {msg}")
        t.alerts.heartbeat(failed=True)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
