# Findings: are momentum and vol_target robust enough to paper trade?

Daily candles, Binance spot, 2018-01-01 to 2026-10-23. Every number is after 0.1% fee + 0.05%
slippage per fill. Out-of-sample (OOS) = 2024-01-01 onward unless stated.

## Verdict

**vol_target survives. momentum does not.** Neither is *proven*, but vol_target passed every
test except one, and that one tells us how to run it.

What survived:

- **vol_target (trend filter + smaller position when the market is wild).** Out-of-sample it
  beat buy & hold on Sharpe, Calmar and drawdown on BTC and ETH, from 2024 and from 2025, and
  in 24 to 25 of 25 parameter settings each time. It also beat buy & hold scaled to the same
  average exposure (the fair benchmark, see step 2), so the edge is not just "held less".
  In 2025-26, while buy & hold lost 14% (BTC) and 29% (ETH), it made +10% and +27%.

What didn't:

- **momentum.** Its BTC numbers look good, but the lookback that looked best in-sample did
  worse out-of-sample on both coins (negative rank correlation), half the lookbacks lost to buy
  & hold, and from 2025 it did no better than simply holding half as much BTC/ETH. The
  lookback 90 result looks like luck.
- **donchian, sma_cross, rsi_reversion**: dropped (overfit, or too few trades).

What we got wrong earlier:

- "Similar return, half the drawdown" was mostly an illusion. Holding ~50% of the coin
  all the time *also* halves the drawdown. Compared fairly (same average exposure) the
  drawdowns are about equal. The real edge is the reverse: **more return for the same
  risk**, from being in the market during the good stretches. Risk-adjusted it's still a win.

The weak spots, honestly:

- **Not statistically significant.** Sharpe advantages of 0.1 to 0.5 over 1 to 3 years are
  inside the noise (bootstrap 90% intervals all cross zero). The case rests on consistency
  across two coins, two test periods and the whole parameter grid, not on any one number.
- **You can't tune it.** Which SMA length works best flipped between test periods (rank
  correlation +0.39/+0.58 from 2024, -0.30/-0.65 from 2025). Use the untuned default
  vol_target(100,30,0.5) and don't optimise it. A blend of SMA lengths looked even better,
  but I tried it after seeing the results, so it needs a clean test (paper trading).
- BTC and ETH move together, so "works on both" is less independent evidence than it sounds.
- Daily data only. Hourly and other coins are untested.
- Out-of-sample means 15 to 23 round trips per coin. That's small.

**Recommendation:** paper trade vol_target(100,30,0.5) on BTC/USDT, with the SMA blend
tracked alongside as a shadow (signals logged, no orders). The goal of paper trading is to
prove the live bot behaves exactly like the backtest. 1 to 3 months is far too short to
judge profitability; the backtest already showed 2 to 3 years can't.

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

## Step 3: a later split (out-of-sample = 2025-01-01 to 2026-10-23)

A harder test: about 22 months, and a falling market (BTC -14%, ETH -29% buy & hold).
In-sample now includes 2024, so this isn't fully independent of step 1; it's a check of
whether the result survives a different period and regime.

| OOS 2025 onward | BTC return | BTC max DD | BTC Sharpe | ETH return | ETH max DD | ETH Sharpe |
|---|---|---|---|---|---|---|
| buy_hold | -14% | -53% | 0.02 | -29% | -68% | 0.07 |
| momentum(90) | -4% | -28% | 0.03 | -8% | -41% | 0.09 |
| matched to momentum | -3% | -27% | 0.03 | -5% | -36% | 0.07 |
| vol_target(100,30,0.5) | **+10%** | -25% | **0.34** | **+27%** | -34% | **0.55** |
| matched to vol_target | -3% | -27% | 0.03 | -3% | -30% | 0.05 |

**momentum: no edge in this period.** It lost less than buy & hold only because it held less;
the exposure-matched benchmark did just as well. The momentum sweep was also inconsistent again
(rank correlation -0.14 BTC, +0.46 ETH, 3 to 4 of 7 lookbacks beating buy & hold).

**vol_target: the strategy family held up, the parameter choice did not.**

- Made money on both coins while buy & hold and the matched benchmark lost. 24 of 25 settings
  on BTC and 25 of 25 on ETH beat buy & hold on Sharpe and Calmar. (In this period Sharpe is
  close to scale-free, so beating buy & hold's Sharpe means beating the matched benchmark too.)
- But the in-sample vs OOS rank correlation turned **negative**: -0.30 BTC, -0.65 ETH. Long
  SMAs (150, 200) were the in-sample favourites on ETH and the worst OOS; sma 50 was the
  reverse. At the 2024 split the same correlation was +0.39 / +0.58.

Average OOS Sharpe by sma length:

| sma | BTC from 2024 | BTC from 2025 | ETH from 2024 | ETH from 2025 |
|---|---|---|---|---|
| 50 | 0.92 | 0.44 | 1.02 | 1.20 |
| 75 | 0.87 | 0.59 | 0.51 | 0.43 |
| 100 | 0.77 | 0.30 | 0.59 | 0.57 |
| 150 | 0.78 | 0.04 | 0.64 | 0.28 |
| 200 | 0.72 | 0.11 | 0.62 | 0.23 |
| buy & hold | 0.69 | 0.02 | 0.34 | 0.07 |

Plain reading: "trend filter + size down when volatile" has worked across coins, periods and
settings. History does **not** reliably say which trend length is best, so tuning it is
pointless. Pick a middle value in advance and leave it alone.

### A candidate that avoids choosing: blend the SMA lengths

Averaging vol_target's positions over sma 50, 75, 100, 150 and 200 (target_vol 0.5) needs
no choice of setting:

| OOS | BTC from 2024 | ETH from 2024 | BTC from 2025 | ETH from 2025 |
|---|---|---|---|---|
| blend: return / max DD / Sharpe | +85% / -27% / 0.87 | +70% / -32% / 0.74 | +10% / -24% / 0.35 | +29% / -28% / 0.62 |
| vol_target(100) | +73% / -34% / 0.78 | +49% / -44% / 0.57 | +10% / -25% / 0.34 | +27% / -34% / 0.55 |
| matched benchmark | +49% / -32% / 0.68 | +18% / -33% / 0.35 | -3% / -29% / 0.02 | -3% / -29% / 0.05 |

It is as good or better in all four cases, with drawdowns at or below the matched benchmark.
**Caveat:** I tried this after seeing the out-of-sample results, so these numbers are not a
clean test. It has no tuned parameter, which limits the damage, but it should be judged on
paper trading, not on this table. Not added to the codebase yet.
