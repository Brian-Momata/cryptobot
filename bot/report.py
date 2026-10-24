"""Charts and markdown reports."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .engine import BacktestResult  # noqa: E402
from .metrics import drawdown_series  # noqa: E402

# Categorical colors in a fixed order (colorblind-checked palette). Benchmark is neutral gray.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
BENCHMARK = "#898781"
INK, INK_2, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7", "#fcfcfb"

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def _style(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=INK_2, labelsize=9)


def plot_equity(results: list[BacktestResult], title: str, path: Path, split: pd.Timestamp | None = None):
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(13, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1.3]}, facecolor=SURFACE
    )
    slot = 0
    for r in results:
        is_bench = r.label.startswith("buy_hold")
        color = BENCHMARK if is_bench else SERIES[slot % len(SERIES)]
        if not is_bench:
            slot += 1
        style = dict(color=color, linewidth=2 if not is_bench else 1.6, linestyle="--" if is_bench else "-")
        ax1.plot(r.equity.index, r.equity.values, label=r.label, **style)
        ax2.plot(r.equity.index, drawdown_series(r.equity).values * 100, **{**style, "linewidth": 1.2})

    ax1.set_yscale("log")
    ax1.set_ylabel("Portfolio value ($, log scale)", color=INK_2, fontsize=10)
    ax1.set_title(title, color=INK, fontsize=13, loc="left", pad=12)
    ax1.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="upper left", bbox_to_anchor=(1.01, 1))
    ax2.set_ylabel("Drawdown (%)", color=INK_2, fontsize=10)
    for ax in (ax1, ax2):
        _style(ax)
        if split is not None:
            ax.axvline(split, color=INK_2, linewidth=1, linestyle=":")
    if split is not None:
        ax1.annotate("  out-of-sample →", xy=(split, 1), xycoords=("data", "axes fraction"),
                     va="top", fontsize=9, color=INK_2)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130, facecolor=SURFACE)
    plt.close(fig)
    return path


def to_markdown(df: pd.DataFrame) -> str:
    """Small markdown table writer (avoids needing the 'tabulate' package)."""
    cols = [df.index.name or ""] + [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for idx, row in df.iterrows():
        lines.append("| " + " | ".join([str(idx)] + [str(v) for v in row.values]) + " |")
    return "\n".join(lines)
