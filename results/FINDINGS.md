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

## Step 2: parameter sweeps (split 2024-01-01)

Two tests of robustness: (a) do neighbouring settings also work, and (b) is the in-sample vs
out-of-sample rank correlation positive (settings that looked good on old data also did well
on new data)?

### momentum: lookback 30, 60, 90, 120, 150, 180, 250

| | BTC | ETH |
|---|---|---|
| OOS Sharpe range across lookbacks | 0.40 to 0.90 | -0.34 to 0.93 |
| Settings beating buy & hold OOS Sharpe | 4 of 7 | 3 of 7 |
| Settings beating buy & hold OOS max drawdown | 7 of 7 | 6 of 7 |
| In-sample vs OOS rank correlation | **-0.39** | **-0.43** |

**Fails.** Rank correlation is negative on both coins: the lookbacks that looked best before
2024 did worse after. On ETH, every long lookback (120 to 250) was roughly flat or lost money OOS, while
short ones (30 to 90) did well. On BTC the top in-sample picks (60, 250, 30) were middling
or poor OOS, while 180 and 90, near the bottom in-sample, did best. 90 wasn't tuned, so its
good OOS result isn't overfitting, but nothing in the history would have told you to pick it.
Its good result looks partly lucky.

### vol_target: sma 50, 75, 100, 150, 200 x target_vol 0.3, 0.4, 0.5, 0.6, 0.8 (25 settings)

| | BTC | ETH |
|---|---|---|
| OOS Sharpe range | 0.68 to 0.96 | 0.47 to 1.04 |
| Settings beating buy & hold OOS Sharpe | 24 of 25 | 25 of 25 |
| Settings beating buy & hold OOS Calmar and max drawdown | 25 of 25 | 25 of 25 |
| In-sample vs OOS rank correlation | **+0.39** | **+0.58** |

**Passes.** There is a broad plateau rather than one lucky spike: the worst of 25 settings is
about level with buy & hold on BTC and well ahead on ETH. Ranking is positively preserved
out-of-sample on both coins. sma is the parameter that matters (50 was best OOS on both).
target_vol mostly scales return and drawdown up and down, as expected. A further sweep of
`vol_window` (10 to 90) changed little (OOS Sharpe 0.67 to 0.81 BTC, 0.48 to 0.68 ETH).

### The fair benchmark: buy & hold at the same exposure

Both strategies are only about half invested on average. **Any** half-invested portfolio has
roughly half buy & hold's drawdown, so "half the drawdown" by itself proves nothing. The new
`fixed_fraction` benchmark holds a constant share equal to the strategy's average exposure,
rebalanced, fees included (`backtest` now prints it automatically).

OOS 2024 onward:

| | Return | Max DD | Sharpe | Calmar |
|---|---|---|---|---|
| BTC momentum(90) | +85% | -28% | 0.82 | 0.88 |
| BTC vol_target(100,30,0.5) | +73% | -34% | 0.78 | 0.63 |
| BTC fixed_fraction(0.55) | +49% | -32% | 0.68 | 0.47 |
| ETH momentum(90) | +66% | -43% | 0.62 | 0.46 |
| ETH vol_target(100,30,0.5) | +49% | -44% | 0.57 | 0.35 |
| ETH fixed_fraction(0.40) | +18% | -33% | 0.35 | 0.18 |

Rows show the matched benchmark at vol_target's exposure. At momentum's own exposure it is
similar: BTC fixed_fraction(0.57) +51%, -33%, Sharpe 0.68; ETH fixed_fraction(0.52) +19%,
-41%, Sharpe 0.35.

This changes the earlier conclusion. At equal exposure the drawdowns are about the **same**
(on ETH the strategies' are actually worse), so the drawdown cut came from holding less, not
from timing. What timing *does* add is return at the same exposure: being in the market
during the good stretches. Risk-adjusted (Sharpe, Calmar), both strategies still come out ahead.

### Is the edge bigger than noise?

Not provably. A block bootstrap (20-day blocks, 2000 resamples) of the OOS Sharpe difference
versus the matched benchmark gives 90% intervals that all cross zero, e.g. BTC vol_target
+0.10 [-0.61, +0.85], ETH vol_target +0.23 [-0.56, +1.12]. Two to three years of daily data
cannot separate a Sharpe edge of 0.1 to 0.3 from luck. The case rests on the edge being
consistent across coins, settings and periods, not on any single number being significant.
(The bootstrap was a one-off analysis script, not part of the repo.)
