"""The paper-trading loop. One call to `tick()` = one hourly run.

Every hour: kill switch, resolve unfinished orders, reconcile with the exchange, risk limits,
heartbeat. Once a day (first tick after the daily candle closes at 00:00 UTC): compute the
target from the last completed candle and trade towards it, exactly like the backtest.

The bot only manages its own slice of the account: `allocated_usdt`, plus whatever coin it
bought. The testnet's other free balances are ignored (recorded as a baseline at --init).
"""
from __future__ import annotations

import pandas as pd

from ..engine import CostModel, run_backtest
from ..strategies import BuyAndHold, Strategy, VolTargetBlend, VolTargetTrend
from .config import Settings
from .risk import Limits, change, check_order, plan_order, position_fraction, price_gap, reconcile_gap

DAY = pd.Timedelta(days=1)
WARMUP_DAYS = 400  # enough history for the longest indicator (sma 200 in the blend)


def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


class Trader:
    def __init__(self, settings: Settings, broker, market, state, alerts, clock=utc_now,
                 strategy: Strategy | None = None, limits: Limits | None = None,
                 costs: CostModel | None = None, log=print):
        self.s, self.broker, self.market, self.state, self.alerts = settings, broker, market, state, alerts
        self.clock, self.log = clock, log
        self.strategy = strategy or VolTargetTrend()
        self.limits = limits or Limits()
        self.costs = costs or CostModel()
        self.symbol = settings.symbol
        self.base, self.quote = settings.symbol.split("/")

    # ---------- setup and manual controls ----------

    def init(self, force: bool = False) -> None:
        if self.state.get("baseline") and not force:
            raise RuntimeError("Already initialised. `--init --force` starts over (e.g. after a testnet reset).")
        bal = self.broker.balances()
        if bal.get(self.quote, 0.0) < self.s.allocated_usdt:
            raise RuntimeError(f"Testnet account has {bal.get(self.quote, 0.0):.2f} {self.quote}, "
                               f"less than ALLOCATED_USDT={self.s.allocated_usdt:.2f}")
        now = self.clock()
        self.state.set("baseline", {self.base: bal.get(self.base, 0.0), self.quote: bal.get(self.quote, 0.0)})
        self.state.set("ledger", {"units": 0.0, "cash": self.s.allocated_usdt})
        self.state.set("started", now.floor("D").isoformat())
        self.state.set("peak_equity", self.s.allocated_usdt)
        for key in ("halted", "last_trade_date", "day_start", "soft_alert_date", "notice_date"):
            self.state.delete(key)
        self.alerts.send(f"Paper trading initialised: {self.s.allocated_usdt:.2f} {self.quote} allocated to "
                         f"{self.strategy.label()} on {self.symbol} (testnet).")

    def resume(self) -> None:
        if self.s.kill_path.exists():
            raise RuntimeError(f"Kill file {self.s.kill_path} exists. Remove it first.")
        halted = self.state.get("halted")
        self.state.delete("halted")
        # Restart the daily-loss and drawdown references, or the same limit would trip again at once.
        self.state.delete("day_start")
        price = self.market.price(self.symbol)
        units, cash = self.account()
        self.state.set("peak_equity", cash + units * price)
        self.alerts.send(f"Resumed by hand (was halted: {halted['reason'] if halted else 'not halted'}).")

    def kill(self, reason: str) -> None:
        """Cancel open orders, sell the bot's coin to USDT, and stay stopped."""
        now = self.clock()
        self.broker.cancel_all(self.symbol)
        sold = ""
        if self.state.get("baseline"):
            self.resolve_pending()
            price = self.market.price(self.symbol)
            units, _ = self.account()
            qty = self.broker.round_amount(self.symbol, max(units, 0.0))
            if qty > 0 and qty * price >= self.broker.min_notional(self.symbol):
                cid = f"vt-kill-{now:%Y%m%d%H%M%S}"
                self.state.add_order(cid, now.date().isoformat(), "sell", qty, now.isoformat())
                fill = self.broker.market_sell(self.symbol, qty, cid)
                self.apply_fill(cid, fill, None)
                sold = f" Sold {qty:g} {self.base} at ~{fill.avg_price:,.2f}."
        self.state.set("halted", {"reason": "killed", "detail": reason, "at": now.isoformat()})
        self.alerts.send(f"KILLED ({reason}).{sold} Bot stays stopped until the kill file is removed "
                         "and `python live.py --resume` is run.")

    def halt(self, reason: str) -> None:
        """Stop trading but KEEP the position (halt-and-hold). Needs a manual --resume."""
        self.broker.cancel_all(self.symbol)
        self.state.set("halted", {"reason": reason, "at": self.clock().isoformat()})
        self.alerts.send(f"HALTED, position kept: {reason}\nNo new orders until you check and run "
                         "`python live.py --resume` (or `--kill` to sell everything).")

    # ---------- the hourly tick ----------

    def tick(self) -> None:
        now = self.clock()
        if self.s.kill_path.exists():
            halted = self.state.get("halted") or {}
            if halted.get("reason") != "killed":
                self.kill(f"kill file {self.s.kill_path.name} found")
            else:
                self.log("Killed: remove the kill file and run --resume to restart.")
            self.alerts.heartbeat()
            return
        if not self.state.get("baseline"):
            raise RuntimeError("Not initialised: run `python live.py --init` first.")

        self.resolve_pending()
        halted = self.state.get("halted")
        if halted:
            self.log(f"Halted since {halted['at']}: {halted['reason']}")
            self.alerts.heartbeat()
            return

        price = self.market.price(self.symbol)
        units, cash = self.account()
        equity = cash + units * price
        if not self.risk_checks(now, units, cash, price, equity):
            self.alerts.heartbeat()
            return
        self.state.add_equity(now.isoformat(), equity, position_fraction(units, cash, price), price)

        if self.state.get("last_trade_date") != now.date().isoformat():
            self.daily(now)
        self.alerts.heartbeat()

    def account(self) -> tuple[float, float]:
        """The bot's coin units and USDT, from exchange balances minus the baseline."""
        bal, base = self.broker.balances(), self.state.get("baseline")
        units = bal.get(self.base, 0.0) - base[self.base]
        cash = self.s.allocated_usdt + bal.get(self.quote, 0.0) - base[self.quote]
        return units, cash

    def risk_checks(self, now, units, cash, price, equity) -> bool:
        lim, ledger = self.limits, self.state.get("ledger")
        gap = reconcile_gap(units, ledger["units"], cash, ledger["cash"], price, equity)
        if gap > lim.max_reconcile_gap:
            self.halt(f"exchange balances differ from the bot's records by {gap:.1%} of equity "
                      f"(exchange {units:g} {self.base} / {cash:.2f} {self.quote}, records {ledger['units']:g} / "
                      f"{ledger['cash']:.2f}). Testnet reset? Then `--init --force`.")
            return False

        today = now.date().isoformat()
        day_start = self.state.get("day_start")
        if not day_start or day_start["date"] != today:
            day_start = {"date": today, "equity": equity}
            self.state.set("day_start", day_start)
        day = change(equity, day_start["equity"])
        if day <= lim.hard_daily_loss:
            self.halt(f"down {day:.1%} today (limit {lim.hard_daily_loss:.0%}).")
            return False
        if day <= lim.soft_daily_loss and self.state.get("soft_alert_date") != today:
            self.state.set("soft_alert_date", today)
            self.alerts.send(f"Warning: down {day:.1%} today (alert at {lim.soft_daily_loss:.0%}, "
                             f"halt at {lim.hard_daily_loss:.0%}). No action taken.")

        peak = max(self.state.get("peak_equity", equity), equity)
        self.state.set("peak_equity", peak)
        dd = change(equity, peak)
        if dd <= lim.max_drawdown:
            self.halt(f"drawdown {dd:.1%} from peak {peak:.2f} (limit {lim.max_drawdown:.0%}): "
                      "outside the strategy's tested behaviour.")
            return False
        return True

    # ---------- orders ----------

    def resolve_pending(self) -> None:
        """An order recorded as pending means we crashed while sending it. Ask the exchange
        whether it arrived before doing anything else, so a day can never trade twice."""
        for o in self.state.orders(status="pending"):
            fill = self.broker.find_order(self.symbol, o["client_id"])
            if fill is None:
                self.state.mark_order(o["client_id"], "not_placed")
                self.alerts.send(f"Order {o['client_id']} never reached the exchange; it will be retried.")
                continue
            self.apply_fill(o["client_id"], fill, None)
            if not o["client_id"].startswith("vt-kill"):
                self.state.set("last_trade_date", o["trade_date"])
            self.alerts.send(f"Recovered order {o['client_id']} after a crash: it did fill "
                             f"({fill.base_delta:+g} {self.base} at {fill.avg_price:,.2f}).")

    def apply_fill(self, client_id: str, fill, mainnet_open: float | None) -> None:
        self.state.fill_order(client_id, fill.base_delta, fill.quote_delta, fill.avg_price, fill.fee_quote,
                              mainnet_open)
        ledger = self.state.get("ledger")
        self.state.set("ledger", {"units": ledger["units"] + fill.base_delta,
                                  "cash": ledger["cash"] + fill.quote_delta})

    def notice_once(self, text: str, today: str) -> None:
        """Alerts that would otherwise repeat every hour go out once a day."""
        if self.state.get("notice_date") != today:
            self.state.set("notice_date", today)
            self.alerts.send(text)
        self.log(text)

    # ---------- the daily decision ----------

    def history_days(self, now) -> int:
        started = pd.Timestamp(self.state.get("started", now.floor("D").isoformat()))
        return max(WARMUP_DAYS, (now - started).days + WARMUP_DAYS)

    def daily(self, now: pd.Timestamp) -> None:
        today = now.date().isoformat()
        candles = self.market.daily_candles(self.symbol, now, self.history_days(now))
        if candles.empty:
            self.notice_once("No daily candles from Binance; not trading.", today)
            return
        candle_time = candles.index[-1]
        age_h = (now - (candle_time + DAY)) / pd.Timedelta(hours=1)
        if age_h > self.limits.max_candle_age_hours:
            self.notice_once(f"Latest daily candle closed {age_h:.0f}h ago (stale data); not trading.", today)
            return
        trade_date = (candle_time + DAY).date().isoformat()
        if trade_date == self.state.get("last_trade_date"):
            return  # today's candle not published yet; next tick will try again

        target = float(self.strategy.target_positions(candles).iloc[-1])
        price = self.market.price(self.symbol)
        testnet_price = self.broker.price(self.symbol)
        units, cash = self.account()
        units = max(units, 0.0)  # dust below zero after fees isn't a short
        current = position_fraction(units, cash, price)
        decision = dict(trade_date=trade_date, candle_time=candle_time.isoformat(), target=target,
                        current=current, price=price, created_at=now.isoformat())

        gap = price_gap(testnet_price, price)
        if gap > self.limits.max_price_gap:
            self.state.add_decision(**decision, action="skip", reason=f"testnet price {gap:.1%} from mainnet")
            self.notice_once(f"Testnet price {testnet_price:,.2f} is {gap:.1%} away from the real market "
                             f"{price:,.2f}; not trading this hour.", today)
            return

        plan = plan_order(target, units, cash, price, self.costs)
        if plan is None:
            self.finish_day(decision, "hold", "change below min_rebalance", now)
            return
        amount = (self.broker.round_cost if plan.side == "buy" else self.broker.round_amount)(self.symbol, plan.amount)
        notional = amount if plan.side == "buy" else amount * price
        if amount <= 0 or notional < self.broker.min_notional(self.symbol):  # e.g. dust left after a sell
            self.finish_day(decision, "hold", f"order {notional:.2f} {self.quote} below exchange minimum", now)
            return
        plan.amount = amount
        problem = check_order(plan, units, cash, price)
        if problem:
            self.state.add_decision(**decision, action="reject", reason=problem)
            self.halt(f"order rejected by safety check: {problem}")
            return
        attempts = self.state.orders(trade_date=trade_date)
        if len(attempts) >= self.limits.max_orders_per_day:
            self.halt(f"{len(attempts)} orders already tried for {trade_date} (limit "
                      f"{self.limits.max_orders_per_day}).")
            return

        cid = f"vt-{trade_date.replace('-', '')}-{len(attempts) + 1}"
        self.state.add_order(cid, trade_date, plan.side, amount, now.isoformat())
        if plan.side == "buy":
            fill = self.broker.market_buy(self.symbol, amount, cid)
        else:
            fill = self.broker.market_sell(self.symbol, amount, cid)
        mainnet_open = self.market.today_open(self.symbol, now)
        self.apply_fill(cid, fill, mainnet_open)
        slip = (fill.avg_price / mainnet_open - 1) if mainnet_open else float("nan")
        self.finish_day(decision, plan.side, f"{cid}: {fill.base_delta:+.6f} {self.base} at {fill.avg_price:,.2f} "
                        f"(fee {fill.fee_quote:.2f} {self.quote}, {slip:+.2%} vs mainnet open)", now)

    def finish_day(self, decision: dict, action: str, reason: str, now: pd.Timestamp) -> None:
        self.state.add_decision(**decision, action=action, reason=reason)
        self.state.set("last_trade_date", decision["trade_date"])
        self.alerts.send(self.daily_report(decision, action, reason, now))

    def daily_report(self, d: dict, action: str, reason: str, now: pd.Timestamp) -> str:
        price = self.market.price(self.symbol)
        units, cash = self.account()
        equity = cash + units * price
        alloc = self.s.allocated_usdt
        lines = [
            f"Daily {d['trade_date']}: {action.upper()}",
            f"Target {d['target']:.0%} in {self.base} (was {d['current']:.0%}). {reason}",
            f"Equity {equity:,.2f} {self.quote} ({change(equity, alloc):+.1%} since start), "
            f"now {position_fraction(units, cash, price):.0%} in {self.base}",
        ]
        try:
            for name, value in self.shadows(now).items():
                lines.append(f"  {name}: {value:,.2f} ({change(value, alloc):+.1%})")
        except Exception as e:  # noqa: BLE001 - the report must never block trading
            lines.append(f"  (shadow ledgers unavailable: {type(e).__name__})")
        return "\n".join(lines)

    # ---------- shadow ledgers (simulated, no orders) ----------

    def shadows(self, now: pd.Timestamp) -> dict[str, float]:
        """What the backtest engine says each pre-registered ledger is worth, from the paper start
        to yesterday's close. See results/FINDINGS.md, 'Pre-registration'."""
        started = pd.Timestamp(self.state.get("started"))
        days = self.history_days(now)
        coin = self.market.daily_candles(self.symbol, now, days)
        other_symbol = f"ETH/{self.quote}" if self.base != "ETH" else f"BTC/{self.quote}"
        other = self.market.daily_candles(other_symbol, now, days)
        alloc = self.s.allocated_usdt

        def value(df, strat, cash):
            window = df[df.index >= started - DAY]  # the candle before the start decides the first trade
            if len(window) < 2:
                return cash
            target = strat.target_positions(df).loc[window.index]
            return float(run_backtest(window, target, self.costs, cash).equity.iloc[-1])

        return {
            f"backtest of {self.strategy.label()}": value(coin, self.strategy, alloc),
            "shadow A (blend)": value(coin, VolTargetBlend(), alloc),
            f"shadow B (50/50 {self.base}+{other_symbol.split('/')[0]})":
                value(coin, VolTargetTrend(), alloc / 2) + value(other, VolTargetTrend(), alloc / 2),
            f"buy & hold {self.base}": value(coin, BuyAndHold(), alloc),
        }
