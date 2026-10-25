# Paper-trading design

**Status: built** (`bot/live/`, `live.py`). To set it up and run it, see [deploy.md](deploy.md).
Where the build differs from this proposal, it says so inline ("Built:").

Goal: run vol_target(100,30,0.5) on BTC/USDT against Binance's spot testnet for 1 to 3 months
and prove the live bot does **exactly what the backtest says it would**. Profit over this
window means nothing (see `results/FINDINGS.md`). What we're testing is the machinery.

Hard rules carried over: testnet only, no real API keys, spot only (0 to 100% of equity),
no leverage, no shorting.

## The key design choice: hourly loop, daily strategy

The strategy was tested on **daily** candles: it decides at the 00:00 UTC close and fills at
the next open. Running it on hourly candles would be a different, untested strategy. So:

- The loop wakes **every hour** to check health, reconcile balances, enforce risk limits and
  send alerts.
- It **trades at most once a day**, just after 00:00 UTC, using the last *completed* daily
  candle, the same way the backtest does.
- Missed runs (PC asleep, crash) are safe. On the next wake it computes the target from the
  latest completed candle and trades once if that day's trade hasn't happened yet.

## Architecture

```
bot/strategies.py   UNCHANGED, shared. live calls strategy.target_positions(candles).iloc[-1]
bot/data.py         reused: daily candles from mainnet's public API (same source as backtest)
bot/live/broker.py  ccxt binance with set_sandbox_mode(True); balances, orders, fills
bot/live/risk.py    pure functions: order sizing, limits, sanity checks (easy to unit test)
bot/live/state.py   SQLite: decisions, orders, fills, daily equity, last-trade date
bot/live/alerts.py  Telegram send (outbound only)
bot/live/trader.py  (built) the tick logic: risk checks, daily decision, shadow ledgers
bot/live/fake.py    (built) in-memory exchange for tests
live.py             CLI; `python live.py --once` runs a single tick (built: a systemd timer runs it hourly)
```

**Signals use mainnet candles, orders go to testnet.** Testnet prices and liquidity are thin
and sometimes far from the real market, and the testnet is wiped periodically. Mainnet
candles keep the signal identical to the backtest. A **shadow ledger** also simulates each fill at
the mainnet open with the backtest's cost model, so we can tell "the strategy did X" apart
from "the testnet did something odd".

The SMA-blend candidate runs as a second shadow ledger: signals and simulated fills
logged, no orders.

## One daily tick, step by step

1. Kill switch present? Stop (see below).
2. Fetch daily candles. Last completed candle older than 26 h? No trade, alert.
3. `target = VolTargetTrend().target_positions(candles).iloc[-1]`
4. Read balances from the exchange (the source of truth, not local state). Compute the
   current position fraction from the mainnet price.
5. `diff = target - current`. Skip if under `min_rebalance` (2%, same as backtest), except a
   full exit to 0.
6. Risk checks (below). Round to Binance lot size; skip if under minimum notional.
7. Market order with `clientOrderId = f"vt-{date}"`. If a retry happens, the exchange
   rejects the duplicate, so one day can never trade twice.
   *Built:* Binance only rejects duplicate IDs among **open** orders, and a filled market
   order isn't open, so this can't be relied on. Instead the order is written to SQLite as
   `pending` *before* it's sent. If the bot crashes, the next tick asks the exchange for that
   client ID: if it filled, it's recorded and the day is done; if it never arrived, the order
   is marked `not_placed` and retried with a new ID (at most 2 attempts a day, then halt).
8. Record the fill. Log slippage against the mainnet open and the shadow ledger's fill.
9. Telegram: what it did and why (target, old/new position, price, fee, slippage).

Hourly ticks run only steps 1, 4 and the risk/health checks, plus a heartbeat.

## Keys and safety

- Spot **testnet** keys from testnet.binance.vision. They can't touch real funds.
- Keys live in `.env` (already gitignored), loaded from environment variables, never logged.
- Startup guard: the bot **refuses to start** unless sandbox mode is on and the API URL is
  the testnet one. A hard assert, not a config flag, so live trading needs a code change.
- For any future live step, which needs its own approval: a separate trade-only key,
  withdrawals disabled, IP whitelist, and a startup check of the key's permissions
  (`GET /sapi/v1/account/apiRestrictions`) that refuses to run if withdrawals are enabled.

## Risk limits

Calibrated from the backtest. vol_target's close-to-close days below -5% happened about
4 to 5 times a year. Below -10% happened once on BTC and five times on ETH in 8.8 years.

| Limit | Value (proposed) | Action |
|---|---|---|
| Allocated capital | fixed USDT amount, e.g. 1,000 testnet USDT | Strategy sizes against this, ignoring the rest of the account |
| Max position | 100% of allocated capital, never more | Reject the order. Spot only, so it never borrows |
| Max single order | 100% of allocated capital | Reject. Catches sizing bugs |
| Max orders per day | 2 | Halt + alert. A bug in a loop is the classic way to bleed fees |
| Daily loss, soft | -5% from the UTC day's starting equity | Alert only |
| Daily loss, hard | -10% | Halt: cancel open orders, no new orders, alert, needs manual restart. Keeps the position |
| Drawdown from peak | -40% (worst BTC backtest drawdown 2018-2026 was -38%) | Halt + alert: the strategy is outside its tested behaviour |
| Price sanity | testnet vs mainnet price > 3% apart | Skip trade, alert |
| Reconciliation | exchange balances differ from expected by > 1% | Halt + alert |

Note: hourly checks see intraday dips, which are deeper than the close-to-close numbers above.

**Decided (2026-10-25): halt and hold.** The backtest never sold on a loss limit, so
flattening would make the bot differ from what was tested, and selling at the bottom of a
crash day is often the worst price. The kill switch is there for when you want out.

## Kill switch

Any one of these stops the bot:

- Create the file `data/KILL`, or run `python live.py --kill`. Next tick (or immediately, if
  run by hand): cancel open orders, sell to USDT, write state, alert, exit. It won't start
  again until the file is removed.
- Telegram `/kill` from your chat ID only (all other senders ignored). Optional; *not built* (it adds a
  polling loop, so it could be phase 2).
- Stop the process (systemd/tmux). With no orders resting, a stopped bot is a safe bot.

## Telegram alerts

Bot token and chat ID in `.env`. Messages:

- every trade (and every skipped trade, with the reason)
- daily summary: equity, position, P&L vs shadow ledgers and vs buy & hold (*Built:* sent with
  the daily decision rather than separately at 00:15)
- every limit breach, error, stale data or reconciliation mismatch
- a dead-man's switch: the bot pings an external heartbeat (e.g. healthchecks.io) hourly,
  which alerts you if the bot itself dies. A dead bot can't send its own alert.

## Where it runs

WSL stops when Windows sleeps. Fine for a first week while you watch it. For 1 to 3 months,
either keep the PC awake or use a small VPS. Run it under systemd with `Restart=on-failure`.

## Tests to write before running it

- Fake broker (in-memory exchange) so the whole loop runs in pytest with no network.
- Live/backtest parity: replay the backtest period through the live loop with the fake broker.
  Targets must match `target_positions` exactly, and orders must match the backtest's trades.
- Risk: each limit triggers and blocks as specified. Duplicate `clientOrderId` doesn't double-trade.
- Startup guard: refuses to start with sandbox off or a non-testnet URL.
- Kill switch: file present → cancels, flattens, exits, won't restart.
- Existing lookahead tests keep passing (the strategy code doesn't change).

## What success looks like after 1 to 3 months

- Every daily target matches what the backtest computes for the same candles.
- Testnet positions track the shadow ledger (gap explained by testnet prices, nothing else).
- Average slippage at or under the 0.05% the backtest assumes (or update the cost model).
- No unhandled errors. Every limit and the kill switch tested at least once on purpose.
- Then, and only then, discuss small live money. That's a separate decision.
