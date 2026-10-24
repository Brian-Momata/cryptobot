"""Command line entry point.

    python run.py fetch    --symbol BTC/USDT --timeframe 1d
    python run.py compare  --symbol BTC/USDT --timeframe 1d
    python run.py backtest --symbol BTC/USDT --strategy sma_cross --param fast=20 --param slow=100
    python run.py sweep    --symbol BTC/USDT --strategy sma_cross --grid fast=10,20,50 --grid slow=100,150,200
    python run.py list

Add --synthetic to any command (except fetch) to run on fake data, no internet needed.
"""
from __future__ import annotations

import argparse
import itertools
import sys

import pandas as pd

from bot import strategies
from bot.data import BARS_PER_YEAR, fetch_ohlcv, load_data, synthetic_data
from bot.engine import CostModel, run_backtest
from bot.metrics import format_table, summarize, yearly_returns
from bot.report import RESULTS_DIR, plot_equity, to_markdown
from bot.robustness import exposure_matched, neighbour_median, rank_corr, share_beating

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)

SUMMARY_COLS = ["total_return", "cagr", "max_drawdown", "sharpe", "calmar", "round_trips", "win_rate",
                "time_in_market", "fees_paid"]


# ---------- helpers ----------

def parse_value(v: str):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def parse_params(items: list[str] | None) -> dict:
    out = {}
    for item in items or []:
        k, v = item.split("=", 1)
        out[k.strip()] = parse_value(v.strip())
    return out


def get_data(args) -> pd.DataFrame:
    if args.synthetic:
        print("⚠  Using SYNTHETIC data: this tests the machinery only, results mean nothing about real markets.\n")
        df = synthetic_data(args.timeframe)
    else:
        df = load_data(args.symbol, args.timeframe)
    if args.start:
        df = df[df.index >= pd.Timestamp(args.start, tz="UTC")]
    if args.end:
        df = df[df.index < pd.Timestamp(args.end, tz="UTC")]
    if len(df) < 300:
        sys.exit(f"Only {len(df)} candles; need at least ~300 for meaningful results.")
    return df


def build(name: str, params: dict, timeframe: str) -> strategies.Strategy:
    cls = strategies.REGISTRY.get(name)
    if cls is None:
        sys.exit(f"Unknown strategy '{name}'. Available: {', '.join(strategies.REGISTRY)}")
    if "bars_per_year" in cls.params and "bars_per_year" not in params:
        params = {**params, "bars_per_year": BARS_PER_YEAR[timeframe]}
    return cls(**params)


def label_of(strat: strategies.Strategy) -> str:
    return strat.label()


MIN_ROUND_TRIPS = 10
BENCHMARKS = ("buy_hold", "fixed_fraction")  # never trade, so never 'thin'


def flag_thin(rows: list[dict]) -> list[str]:
    """Strategies with too few trades: their stats are mostly luck."""
    return [r["strategy"] for r in rows
            if not r["strategy"].startswith(BENCHMARKS) and r["round_trips"] < MIN_ROUND_TRIPS]


def costs_from(args) -> CostModel:
    return CostModel(fee=args.fee, slippage=args.slippage, min_rebalance=args.min_rebalance)


def evaluate(df, target, costs, cash, label, bpy, split):
    """Backtest the full period plus the in-sample / out-of-sample halves.

    The strategy's signals are computed once on all the data (so indicators are warmed up),
    then each period is simulated separately starting from cash.
    """
    full = run_backtest(df, target, costs, cash, label)
    out = {"full": (full, summarize(full, bpy))}
    if split is not None:
        ins_df, oos_df = df[df.index < split], df[df.index >= split]
        if len(ins_df) > 50 and len(oos_df) > 50:
            ins = run_backtest(ins_df, target.loc[ins_df.index], costs, cash, label)
            oos = run_backtest(oos_df, target.loc[oos_df.index], costs, cash, label)
            out["in_sample"] = (ins, summarize(ins, bpy))
            out["out_of_sample"] = (oos, summarize(oos, bpy))
    return out


def tag(args) -> str:
    sym = "SYNTHETIC" if args.synthetic else args.symbol.replace("/", "-")
    return f"{sym}_{args.timeframe}"


# ---------- commands ----------

def cmd_list(args):
    for name, cls in strategies.REGISTRY.items():
        doc = (cls.__doc__ or "").strip().split("\n")[0]
        print(f"{name:15} {cls.params}\n{'':15} {doc}\n")


def cmd_fetch(args):
    fetch_ohlcv(args.symbol, args.timeframe, args.since)


def cmd_compare(args):
    df = get_data(args)
    bpy = BARS_PER_YEAR[args.timeframe]
    split = pd.Timestamp(args.split, tz="UTC") if args.split else None
    costs = costs_from(args)
    print(f"{tag(args)}: {len(df):,} candles, {df.index[0].date()} → {df.index[-1].date()}")
    print(f"Costs: fee {costs.fee:.2%}, slippage {costs.slippage:.2%} per fill. Starting cash ${args.cash:,.0f}\n")

    rows = {"full": [], "in_sample": [], "out_of_sample": []}
    fulls = []
    yearly = {}
    for name in strategies.REGISTRY:
        strat = build(name, {}, args.timeframe)
        label = label_of(strat)
        res = evaluate(df, strat.target_positions(df), costs, args.cash, label, bpy, split)
        for period, (r, s) in res.items():
            rows[period].append(s)
        fulls.append(res["full"][0])
        yearly[label] = yearly_returns(res["full"][0].equity, args.cash)

    md = [f"# Strategy comparison: {tag(args)}", "",
          f"{len(df):,} candles, {df.index[0].date()} to {df.index[-1].date()}. "
          f"Fee {costs.fee:.2%} + slippage {costs.slippage:.2%} per fill. Start ${args.cash:,.0f}.", ""]
    if args.synthetic:
        md += ["> ⚠ SYNTHETIC DATA. These numbers only prove the code runs.", ""]

    titles = {"full": "Full period", "in_sample": f"In-sample (before {args.split})",
              "out_of_sample": f"Out-of-sample ({args.split} onwards) ← the one that matters"}
    for period, title in titles.items():
        if not rows[period]:
            continue
        table = format_table(rows[period])[SUMMARY_COLS]
        thin = flag_thin(rows[period])
        note = (f"⚠ Fewer than {MIN_ROUND_TRIPS} round trips, too few to judge: {', '.join(thin)}" if thin else "")
        print(f"=== {title} ===\n{table.to_string()}\n{note + chr(10) if note else ''}")
        md += [f"## {title}", "", to_markdown(table), ""] + ([f"> {note}", ""] if note else [])

    yt = pd.DataFrame(yearly).T
    yt_fmt = yt.map(lambda v: f"{v * 100:,.0f}%")
    yt_fmt.index.name = "strategy"
    print(f"=== Return by calendar year ===\n{yt_fmt.to_string()}\n")
    md += ["## Return by calendar year", "",
           "Shows how each strategy behaves in bull, bear and sideways years.", "", to_markdown(yt_fmt), ""]

    chart = plot_equity(fulls, f"{tag(args)}: equity and drawdown (after fees)",
                        RESULTS_DIR / f"compare_{tag(args)}.png", split)
    md += [f"![equity](compare_{tag(args)}.png)", "", HOW_TO_READ]
    report = RESULTS_DIR / f"compare_{tag(args)}.md"
    report.write_text("\n".join(md), encoding="utf-8")
    print(f"Report: {report}\nChart:  {chart}")


def cmd_backtest(args):
    df = get_data(args)
    bpy = BARS_PER_YEAR[args.timeframe]
    split = pd.Timestamp(args.split, tz="UTC") if args.split else None
    costs = costs_from(args)
    strat = build(args.strategy, parse_params(args.param), args.timeframe)
    label = label_of(strat)
    target = strat.target_positions(df)
    res = evaluate(df, target, costs, args.cash, label, bpy, split)
    bench = evaluate(df, strategies.BuyAndHold().target_positions(df), costs, args.cash, "buy_hold", bpy, split)

    periods = {"full": df}
    if split is not None:
        periods |= {"in_sample": df[df.index < split], "out_of_sample": df[df.index >= split]}
    for period, (r, s) in res.items():
        matched = exposure_matched(periods[period], r, costs, args.cash)
        table = format_table([s, bench[period][1], summarize(matched, bpy)])[SUMMARY_COLS]
        warn = f"⚠ Only {s['round_trips']} round trips: too few to judge.\n" if flag_thin([s]) else ""
        print(f"=== {period} ===\n{table.to_string()}\n{warn}")

    full = res["full"][0]
    trades_path = RESULTS_DIR / f"trades_{args.strategy}_{tag(args)}.csv"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    full.trades.to_csv(trades_path, index=False)
    chart = plot_equity([full, bench["full"][0]], f"{label} vs buy & hold ({tag(args)})",
                        RESULTS_DIR / f"backtest_{args.strategy}_{tag(args)}.png", split)
    print(f"Trades: {trades_path}\nChart:  {chart}")


def cmd_sweep(args):
    """Try every parameter combination on IN-SAMPLE data only, rank by Sharpe,
    then reveal how the top picks did out-of-sample."""
    df = get_data(args)
    bpy = BARS_PER_YEAR[args.timeframe]
    split = pd.Timestamp(args.split, tz="UTC")
    costs = costs_from(args)
    grid = {k: [parse_value(x) for x in v.split(",")] for k, v in (g.split("=", 1) for g in args.grid)}
    combos = [dict(zip(grid, vals)) for vals in itertools.product(*grid.values())]
    print(f"Testing {len(combos)} combinations of {args.strategy} (tuned on data before {args.split})...\n")

    rows = []
    for params in combos:
        strat = build(args.strategy, params, args.timeframe)
        res = evaluate(df, strat.target_positions(df), costs, args.cash, label_of(strat), bpy, split)
        if "in_sample" not in res:
            sys.exit("Split leaves too little data on one side.")
        ins, oos = res["in_sample"][1], res["out_of_sample"][1]
        rows.append({"params": label_of(strat), **params,
                     "IS_sharpe": ins["sharpe"], "IS_cagr": ins["cagr"], "IS_maxdd": ins["max_drawdown"],
                     "OOS_sharpe": oos["sharpe"], "OOS_cagr": oos["cagr"], "OOS_maxdd": oos["max_drawdown"],
                     "OOS_calmar": oos["calmar"], "OOS_trips": oos["round_trips"]})

    bh = evaluate(df, strategies.BuyAndHold().target_positions(df), costs, args.cash, "buy_hold", bpy, split)
    bh_oos = bh["out_of_sample"][1]
    table = pd.DataFrame(rows).set_index("params")
    table["IS_sharpe_nbr"] = neighbour_median(table, list(grid), "IS_sharpe")
    table["OOS_sharpe_nbr"] = neighbour_median(table, list(grid), "OOS_sharpe")
    table = table.sort_values("IS_sharpe", ascending=False)
    shown = table.drop(columns=list(grid)).head(args.top).copy()
    for c in shown.columns:
        if "sharpe" in c or "calmar" in c:
            shown[c] = shown[c].map(lambda v: f"{v:.2f}")
        elif c != "OOS_trips":
            shown[c] = shown[c].map(lambda v: f"{v * 100:.1f}%")
    print(shown.to_string())
    print(f"\nBuy & hold: IS sharpe {bh['in_sample'][1]['sharpe']:.2f}, OOS sharpe {bh_oos['sharpe']:.2f}, "
          f"OOS cagr {bh_oos['cagr'] * 100:.1f}%, OOS max DD {bh_oos['max_drawdown'] * 100:.1f}%, "
          f"OOS calmar {bh_oos['calmar']:.2f}")
    print(f"\nIn-sample vs out-of-sample rank correlation: {rank_corr(table):.2f}")
    print("  Near 0 or negative = the 'best' settings were luck (overfit). Want this clearly positive.")
    print(f"Settings beating buy & hold OOS: sharpe {share_beating(table, 'OOS_sharpe', bh_oos['sharpe']):.0%}, "
          f"calmar {share_beating(table, 'OOS_calmar', bh_oos['calmar']):.0%}, "
          f"max DD {share_beating(table, 'OOS_maxdd', bh_oos['max_drawdown']):.0%}")
    print("  *_nbr = median over the setting and its grid neighbours. A good setting with bad neighbours is luck.")
    out = RESULTS_DIR / f"sweep_{args.strategy}_{'-'.join(grid)}_{tag(args)}_split{args.split}.csv"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    table.to_csv(out)
    print(f"\nAll results: {out}")


HOW_TO_READ = """## How to read this

- **Out-of-sample is what matters.** In-sample is where ideas were found; out-of-sample is a test the strategy never saw.
- **CAGR** is the yearly growth rate. **Max drawdown** is the worst fall from a peak; ask yourself if you could sit through it.
- **Sharpe** is return per unit of volatility (above 1 is good, above 2 is suspicious).
- **Calmar** is CAGR divided by max drawdown: return per unit of pain.
- A strategy only earns its place if it beats buy & hold on risk-adjusted terms (Sharpe, Calmar, drawdown) out-of-sample, after fees.
"""


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--symbol", default="BTC/USDT")
    common.add_argument("--timeframe", default="1d", choices=list(BARS_PER_YEAR))
    common.add_argument("--synthetic", action="store_true", help="use fake data (no internet needed)")
    common.add_argument("--start", help="ignore data before this date, e.g. 2019-01-01")
    common.add_argument("--end", help="ignore data from this date on")
    common.add_argument("--split", default="2024-01-01", help="in-sample / out-of-sample boundary")
    common.add_argument("--cash", type=float, default=1000.0)
    common.add_argument("--fee", type=float, default=0.001, help="fee per fill (0.001 = 0.1%%)")
    common.add_argument("--slippage", type=float, default=0.0005, help="slippage per fill (0.0005 = 0.05%%)")
    common.add_argument("--min-rebalance", type=float, default=0.02, dest="min_rebalance")

    p = argparse.ArgumentParser(description="Crypto strategy backtester")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show strategies and their default params")

    f = sub.add_parser("fetch", help="download candles from Binance", parents=[common])
    f.add_argument("--since", default="2018-01-01")

    sub.add_parser("compare", help="run every strategy with default params", parents=[common])

    b = sub.add_parser("backtest", help="run one strategy", parents=[common])
    b.add_argument("--strategy", required=True)
    b.add_argument("--param", action="append", help="e.g. --param fast=20 (repeatable)")

    s = sub.add_parser("sweep", help="tune params in-sample, check out-of-sample", parents=[common])
    s.add_argument("--strategy", required=True)
    s.add_argument("--grid", action="append", required=True, help="e.g. --grid fast=10,20,50 (repeatable)")
    s.add_argument("--top", type=int, default=10)

    args = p.parse_args()
    {"list": cmd_list, "fetch": cmd_fetch, "compare": cmd_compare,
     "backtest": cmd_backtest, "sweep": cmd_sweep}[args.cmd](args)


if __name__ == "__main__":
    main()
