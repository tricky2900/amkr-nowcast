"""Static PNG charts. Deliberately plain: these get pasted into notes and decks."""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from common import DATA, OUTPUT, log, quarter_sort_key, safe_read

LOG = log("charts")
plt.rcParams.update({
    "font.family": "sans-serif", "font.size": 9, "axes.grid": True,
    "grid.alpha": 0.25, "axes.spines.top": False, "axes.spines.right": False,
    "figure.dpi": 140,
})
INK, ACCENT, WARM = "#1F3864", "#C00000", "#ED7D31"


def _q(idx):
    return sorted(idx, key=quarter_sort_key)


def chart_yoy(quarterly: pd.DataFrame) -> None:
    piv = quarterly.pivot_table(index="quarter", columns="series_id", values="yoy")
    if "AMKR" not in piv.columns:
        return
    order = _q(piv.index)
    piv = piv.loc[order]
    peers = [c for c in ("OSAT_COMPOSITE", "TSMC", "ASE") if c in piv.columns]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(range(len(piv)), piv["AMKR"] * 100, color=ACCENT, lw=2.4, marker="o",
            ms=4, label="AMKR", zorder=5)
    for i, p in enumerate(peers):
        ax.plot(range(len(piv)), piv[p] * 100, lw=1.5, alpha=0.85,
                color=[INK, WARM, "#7F7F7F"][i % 3], label=p)
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xticks(range(0, len(piv), max(1, len(piv) // 12)))
    ax.set_xticklabels([piv.index[i] for i in range(0, len(piv), max(1, len(piv) // 12))],
                       rotation=45, ha="right")
    ax.set_ylabel("YoY revenue growth (%)")
    ax.set_title("AMKR vs Taiwan indicators - quarterly YoY", loc="left", fontweight="bold")
    ax.legend(frameon=False, ncol=4, loc="upper left")
    fig.tight_layout()
    fig.savefig(OUTPUT / "chart_yoy.png")
    plt.close(fig)


def chart_leadlag(ll: pd.DataFrame) -> None:
    if ll.empty:
        return
    d = ll.sort_values("corr").tail(14)
    fig, ax = plt.subplots(figsize=(8, max(4, 0.35 * len(d))))
    colors = [ACCENT if v > 0.7 else INK if v > 0.4 else "#A6A6A6" for v in d["corr"]]
    ax.barh(range(len(d)), d["corr"], color=colors)
    ax.set_yticks(range(len(d)))
    ax.set_yticklabels([f"{r.series_id}  (lead {int(r.lead_months)}m)" for r in d.itertuples()])
    ax.set_xlabel("Correlation with AMKR quarterly YoY")
    ax.set_title("Best lead/lag correlation by series", loc="left", fontweight="bold")
    ax.set_xlim(min(0, d["corr"].min() * 1.1), 1)
    for i, v in enumerate(d["corr"]):
        ax.text(v + 0.015, i, f"{v:.2f}", va="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(OUTPUT / "chart_lead_lag.png")
    plt.close(fig)


def chart_backtest() -> None:
    bt = safe_read(OUTPUT / "backtest_predictions.csv")
    if bt.empty:
        return
    fig, ax = plt.subplots(figsize=(9, 4.2))
    x = range(len(bt))
    ax.plot(x, bt["actual"] * 100, color=ACCENT, lw=2.2, marker="o", ms=4, label="Actual")
    ax.plot(x, bt["predicted"] * 100, color=INK, lw=1.8, ls="--", marker="s", ms=3.5,
            label="Predicted (out-of-sample)")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xticks(list(x))
    ax.set_xticklabels(bt["quarter"], rotation=45, ha="right")
    ax.set_ylabel("AMKR YoY (%)")
    ax.set_title("Rolling-origin backtest - refit at each step, no lookahead",
                 loc="left", fontweight="bold")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(OUTPUT / "chart_backtest.png")
    plt.close(fig)


def chart_scenarios() -> None:
    sc = safe_read(OUTPUT / "scenarios.csv")
    if sc.empty:
        return
    top = sc.iloc[0]
    fig, ax = plt.subplots(figsize=(8, 3.4))
    ax.hlines(1, top.bear_usdm, top.bull_usdm, color=INK, lw=9, alpha=0.25,
              label="Bear-bull range")
    if top.guide_low_usdm == top.guide_low_usdm:
        ax.hlines(1, top.guide_low_usdm, top.guide_high_usdm, color=WARM, lw=9,
                  alpha=0.85, label="Company guidance")
        ax.plot(top.guide_mid_usdm, 1, "|", color="black", ms=26, mew=2,
                label="Guidance midpoint")
    ax.plot(top.base_usdm, 1, "o", color=ACCENT, ms=13, zorder=6,
            label=f"Model base ({top.predictor})")
    ax.set_ylim(0.6, 1.5)
    ax.set_yticks([])
    ax.set_xlabel("Net sales (USD millions)")
    ax.set_title(f"{top.quarter} nowcast - {int(top.months_reported)} of 3 months reported",
                 loc="left", fontweight="bold")
    ax.legend(frameon=False, loc="upper center", ncol=2, fontsize=8)
    for v, lab in [(top.bear_usdm, "bear"), (top.base_usdm, "base"), (top.bull_usdm, "bull")]:
        ax.annotate(f"{lab}\n${v:,.0f}m", (v, 0.82), ha="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(OUTPUT / "chart_scenarios.png")
    plt.close(fig)


def run() -> None:
    OUTPUT.mkdir(exist_ok=True)
    quarterly = safe_read(DATA / "master_quarterly.csv")
    if not quarterly.empty:
        chart_yoy(quarterly)
    ll = safe_read(OUTPUT / "lead_lag.csv")
    if not ll.empty:
        chart_leadlag(ll)
    chart_backtest()
    chart_scenarios()
    LOG.info("charts written to %s", OUTPUT)


if __name__ == "__main__":
    run()
