# cryptobot: backtester

Step 1 of the trading bot: test strategies on years of real BTC/ETH data, with realistic
fees and slippage, before any money is involved.

The strategy code written here is the same code the paper-trading and live bot will run later.

## Setup (WSL/Ubuntu)

```bash
cd cryptobot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest            # 42 tests, should all pass
```

## Use it

```bash
# 1. Download data (public Binance API, no account or API key needed)
python run.py fetch --symbol BTC/USDT --timeframe 1d
python run.py fetch --symbol ETH/USDT --timeframe 1d
python run.py fetch --symbol BTC/USDT --timeframe 1h     # optional, ~70k candles, a few minutes

# 2. Compare every strategy against buy & hold
python run.py compare --symbol BTC/USDT --timeframe 1d
python run.py compare --symbol ETH/USDT --timeframe 1d

# 3. Dig into one strategy with custom settings
python run.py backtest --symbol BTC/USDT --strategy sma_cross --param fast=20 --param slow=100

# 4. Tune settings honestly (tuned on old data, judged on new data)
python run.py sweep --symbol BTC/USDT --strategy donchian --grid entry=10,20,40,55 --grid exit=5,10,20

# 5. See how often it wins year to year (every 12-month window, one starting each month)
python run.py rolling --symbol BTC/USDT --strategy vol_target

python run.py list         # strategies and their settings
```

Reports, charts and trade logs land in `results/`. Add `--synthetic` to any command to run on
fake data without internet (only proves the code works).

If Binance is unreachable from your network, use another exchange's identical API: edit
`exchange="binance"` in `bot/data.py` to `"bybit"` or `"kraken"` (Kraken uses `BTC/USD`).

Useful flags: `--split 2024-01-01` (in-sample / out-of-sample boundary), `--start 2019-01-01`,
`--fee 0.001`, `--slippage 0.0005`, `--cash 1000`.

## How it avoids lying to you

| Trap | What this does about it |
|---|---|
| Lookahead (using future data) | Decisions at a candle's close execute at the **next** candle's open. A test checks every strategy gives identical signals when future candles are removed, and that this check catches a strategy that cheats. The simulator is checked against an independent formula. |
| Ignoring costs | 0.1% fee + 0.05% slippage on every fill, built into every number. |
| Overfitting | Data is split: tune before 2024, judge 2024 onward. `sweep` reports whether the best in-sample settings stayed good out-of-sample. |
| Luck from few trades | Strategies with fewer than 10 round trips are flagged. |
| Ignoring the pain | Max drawdown, Calmar and per-year returns shown next to profit. |
| No baseline | Buy & hold is always in the table. `backtest` also shows buy & hold scaled to the strategy's average exposure, since holding less always shrinks drawdowns. |
| Lucky single setting | `sweep` shows each setting's neighbourhood median (`*_nbr`) and the share of settings that beat buy & hold. |

## Strategies

| Name | Idea |
|---|---|
| `buy_hold` | Benchmark. Buy once, never sell. |
| `fixed_fraction` | Benchmark. Always hold a fixed share (default 50%), rest in cash. |
| `sma_cross` | Trend: hold while the 50-day average is above the 200-day. |
| `momentum` | Trend: hold if price is above where it was 90 candles ago. |
| `donchian` | Breakout: buy a new 20-candle high, sell a new 10-candle low. |
| `rsi_reversion` | Mean reversion: buy oversold dips in an uptrend, sell the bounce. |
| `vol_target` | Trend + sizing: in an uptrend, hold less when the market is wild, more when calm. |
| `vol_target_blend` | vol_target averaged over sma 50, 75, 100, 150 and 200, so no trend length is chosen. |

Add your own in `bot/strategies.py`: subclass `Strategy`, implement `target_positions(df)`
returning 0 to 1 per candle, add it to `REGISTRY`. The lookahead test covers it automatically.

## Reading results

- **Out-of-sample** is the only table that counts.
- A strategy earns its place only if it beats buy & hold on **risk-adjusted** terms
  (Sharpe, Calmar, max drawdown) out-of-sample, after costs, on both BTC and ETH.
- Also compare against holding the same average amount (`fixed_fraction`, shown by `backtest`):
  holding less always shrinks drawdowns, and that isn't skill.
- A good full-period number can hide a strategy that only wins in one or two crash years. Check `rolling`.
- Sharpe above 2 on a simple strategy usually means a bug or luck, not genius.
- Expect most strategies to fail. That's the backtester doing its job.

## Project layout

```
bot/data.py        download + cache candles, synthetic data
bot/strategies.py  strategies (shared with the future live bot)
bot/engine.py      simulator: next-open fills, fees, slippage, spot only
bot/metrics.py     CAGR, drawdown, Sharpe, win rate, yearly returns
bot/robustness.py  sweep neighbourhoods, rank correlation, exposure-matched benchmark
bot/report.py      charts + markdown
run.py             command line
tests/             accounting, lookahead and fetch tests
```

## Next steps

1. Run `compare` on BTC and ETH, daily and hourly.
2. Shortlist anything that beats buy & hold out-of-sample on risk-adjusted terms.
3. Paper trade the survivors on Binance testnet for 1 to 3 months. Results so far: `results/FINDINGS.md`; design: `docs/paper_trading_design.md`.
4. Only then, live with small money, hard loss limits and a kill switch.

Not financial advice. Past performance, especially backtested, doesn't guarantee future results.
