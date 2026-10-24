# Findings: are momentum and vol_target robust enough to paper trade?

Daily candles, Binance spot, 2018-01-01 to 2026-10-23. Every number is after 0.1% fee + 0.05%
slippage per fill. Out-of-sample (OOS) = 2024-01-01 onward unless stated.

## Step 1: does it hold up on ETH?

Out-of-sample, 2024-01-01 to 2026-10-23:

| | BTC return | BTC max DD | BTC Sharpe | ETH return | ETH max DD | ETH Sharpe |
|---|---|---|---|---|---|---|
| buy_hold | +84% | -53% | 0.69 | +1% | -68% | 0.34 |
| momentum(90) | +85% | -28% | 0.82 | +66% | -43% | 0.62 |
| vol_target(100,30,0.5) | +73% | -34% | 0.78 | +49% | -44% | 0.57 |
| donchian(20,10) | +35% | -38% | 0.50 | -45% | -63% | -0.38 |

Both candidates hold up on ETH, a market they were never designed on. On ETH buy & hold went
nowhere with a 68% drawdown, and both strategies made money with about two thirds of that
drawdown. Donchian's collapse repeats on ETH, which confirms it was overfit.

Note: only 15 (momentum) and 23 (vol_target) round trips per coin OOS. That is enough to
not be pure noise, but a long way from proof.
