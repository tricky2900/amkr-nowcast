"""Guidance track record, and whether the current guide looks conservative.

The question worth asking before a print is not "what number will management
guide to" - that is a behavioural decision the Taiwan data cannot observe. It
is: GIVEN the guide, does the peer data imply Amkor lands high or low in it?

Three reference points, and keeping them distinct is the whole point:

  1. Guidance midpoint      what management said
  2. Bias-adjusted guidance where management's own history says they land
                            within their range
  3. Peer-implied           what Taiwan monthly revenue implies

(1) vs (2) measures how management guides. (2) vs (3) is the actual signal: the
peer data disagreeing with a bias-adjusted expectation is more informative than
it disagreeing with a raw midpoint, because a firm that habitually lands in the
upper half is not "beating" when it does so again.

POSITION IN RANGE is the core statistic: (actual - low) / (high - low). 0.5 is
the midpoint, 1.0 the top of the guide, >1.0 a beat above the range. It is
scale-free, so quarters of very different size are comparable, and it is
median-based so one blowout quarter does not set the expectation.

SAMPLE SIZE: this needs a decent run of quarters to mean anything. Under 8 the
module reports the track record but refuses to compute a bias adjustment - a
"typical position" from four observations is an anecdote with a decimal point.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import DATA, OUTPUT, log, quarter_sort_key, safe_read
from manual_sources import load_guidance

LOG = log("guidance_analysis")
MIN_FOR_BIAS = 8


def track_record() -> pd.DataFrame:
    guidance = load_guidance()
    actual = safe_read(DATA / "raw" / "amkr_quarterly.csv")
    if guidance.empty or actual.empty:
        raise RuntimeError("need both guidance and actual revenue")

    lvl = dict(zip(actual.period, actual.value))
    rows = []
    for _, g in guidance.iterrows():
        a = lvl.get(g.quarter)
        if a is None:
            continue                      # quarter not reported yet
        lo, hi = float(g.guide_low_usdm), float(g.guide_high_usdm)
        mid = (lo + hi) / 2
        width = hi - lo
        rows.append({
            "quarter": g.quarter,
            "guide_low_usdm": lo, "guide_mid_usdm": mid, "guide_high_usdm": hi,
            "actual_usdm": a,
            "position_in_range": (a - lo) / width if width else np.nan,
            "vs_mid_pct": a / mid - 1,
            "above_mid": a > mid,
            "above_high": a > hi,
            "below_low": a < lo,
        })
    if not rows:
        raise RuntimeError("no quarters have both guidance and a reported actual")
    return (pd.DataFrame(rows)
            .sort_values("quarter", key=lambda c: c.map(quarter_sort_key)))


def summarise(tr: pd.DataFrame) -> dict:
    n = len(tr)
    s = {
        "n_quarters": n,
        "median_position_in_range": float(tr.position_in_range.median()),
        "mean_position_in_range": float(tr.position_in_range.mean()),
        "pct_above_midpoint": float(tr.above_mid.mean()),
        "pct_above_high_end": float(tr.above_high.mean()),
        "pct_below_low_end": float(tr.below_low.mean()),
        "median_vs_mid_pct": float(tr.vs_mid_pct.median()),
        "vs_mid_sd_pct": float(tr.vs_mid_pct.std(ddof=1)) if n > 1 else np.nan,
        "bias_adjustment_usable": n >= MIN_FOR_BIAS,
    }
    LOG.info("track record: %d quarters, median position %.2f of range, "
             "%.0f%% landed above the midpoint",
             n, s["median_position_in_range"], 100 * s["pct_above_midpoint"])
    if n < MIN_FOR_BIAS:
        LOG.warning("only %d quarters - too few for a bias adjustment "
                    "(need %d). Reporting the record, not applying it.",
                    n, MIN_FOR_BIAS)
    return s


def current_read(tr: pd.DataFrame, s: dict) -> pd.DataFrame:
    """Compare the live guide against both management's habit and the peers."""
    guidance = load_guidance()
    scen = safe_read(OUTPUT / "scenarios.csv")
    if scen.empty:
        LOG.warning("no scenarios.csv - cannot add the peer-implied column")
        return pd.DataFrame()

    target = scen["quarter"].iloc[0]
    g = guidance[guidance.quarter == target]
    if g.empty:
        LOG.warning("no guidance on file for %s", target)
        return pd.DataFrame()

    lo, hi = float(g.guide_low_usdm.iloc[0]), float(g.guide_high_usdm.iloc[0])
    mid, width = (lo + hi) / 2, hi - lo
    peer = float(scen["base_usdm"].iloc[0])
    peer_sd = float(scen["resid_sd_pp"].iloc[0]) * float(scen["prior_year_revenue_usdm"].iloc[0])
    peer_pos = (peer - lo) / width if width else np.nan

    row = {
        "quarter": target,
        "guide_low_usdm": lo, "guide_mid_usdm": mid, "guide_high_usdm": hi,
        "peer_implied_usdm": peer,
        "peer_implied_sd_usdm": peer_sd,
        "peer_implied_position_in_range": peer_pos,
        "peer_vs_mid_pct": peer / mid - 1,
    }
    if s["bias_adjustment_usable"]:
        adj = lo + s["median_position_in_range"] * width
        row["bias_adjusted_guide_usdm"] = adj
        row["peer_vs_bias_adjusted_pct"] = peer / adj - 1
        # Is the gap meaningful next to the model's own noise?
        row["gap_vs_bias_adj_in_sd"] = (peer - adj) / peer_sd if peer_sd else np.nan
        LOG.info("%s: guide mid $%.0fm | bias-adjusted $%.0fm | peer-implied $%.0fm",
                 target, mid, adj, peer)
        LOG.info("  peer vs bias-adjusted: %+.1f%% (%.1f model SDs)",
                 100 * row["peer_vs_bias_adjusted_pct"], row["gap_vs_bias_adj_in_sd"])
        if abs(row["gap_vs_bias_adj_in_sd"]) < 1.0:
            LOG.warning("  gap is inside one model SD - not a signal, just noise")
    else:
        row["bias_adjusted_guide_usdm"] = np.nan
        LOG.info("%s: guide mid $%.0fm | peer-implied $%.0fm (%+.1f%%), "
                 "no bias adjustment yet", target, mid, peer,
                 100 * row["peer_vs_mid_pct"])
    return pd.DataFrame([row])


def run() -> dict:
    tr = track_record()
    s = summarise(tr)
    OUTPUT.mkdir(exist_ok=True)
    tr.to_csv(OUTPUT / "guidance_track_record.csv", index=False)
    pd.DataFrame([s]).to_csv(OUTPUT / "guidance_summary.csv", index=False)
    cur = current_read(tr, s)
    if not cur.empty:
        cur.to_csv(OUTPUT / "guidance_current_read.csv", index=False)
    return {"track_record": tr, "summary": s, "current": cur}


if __name__ == "__main__":
    run()
