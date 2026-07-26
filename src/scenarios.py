"""Guidance-anchored bull / base / bear estimate for the quarter in progress.

The framing, per spec: Amkor's own guidance range defines the floor and ceiling
whenever guidance exists, and the model supplies the point estimate inside (or
outside) it. Concretely

    base  = model point estimate
    bull  = max(guidance high, model + z * sigma)
    bear  = min(guidance low,  model - z * sigma)

so the band never claims more precision than the company itself does, but it
does widen when the peer data disagrees with guidance. The number an analyst
actually trades on is `delta_vs_guide_mid`: how far the Taiwan data implies
Amkor lands from its own midpoint. With no guidance on file the scenarios fall
back to pure model percentiles.

MATCHED-HORIZON REFIT
The naive version of this uses a model fit on complete quarters and then feeds
it two months of data, which quietly understates the error - two months is a
noisier read on a quarter than three. Instead, when only n months of the
current quarter have printed, the model is refit on the first n months of every
historical quarter. The n=2 model is genuinely worse than the n=3 model, and
this makes that visible in the bands rather than hiding it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm

from common import (DATA, OUTPUT, load_config, log, month_to_quarter,
                    quarter_sort_key, safe_read, shift_month)
from manual_sources import load_guidance

LOG = log("scenarios")
TARGET = "AMKR"


def months_available(monthly: pd.DataFrame, sid: str, lead: int, quarter: str) -> int:
    """How many months of `quarter` exist AFTER applying the lead shift.

    This has to be measured on the shifted series, not the calendar quarter.
    That is the whole point of a leading indicator: at lead 2 a quarter can be
    fully populated before its own calendar months have finished printing.
    """
    s = monthly[monthly.series_id == sid].copy()
    if s.empty:
        return 0
    s["period"] = s["period"].map(lambda p: shift_month(p, lead))
    s["quarter"] = s["period"].map(month_to_quarter)
    return int(min(3, (s["quarter"] == quarter).sum()))


def partial_quarter_yoy(monthly: pd.DataFrame, sid: str, lead: int, n_months: int) -> pd.Series:
    """YoY using only the first n months of each quarter, for every quarter."""
    s = monthly[monthly.series_id == sid].copy()
    if s.empty:
        return pd.Series(dtype=float)
    s["period"] = s["period"].map(lambda p: shift_month(p, lead))
    s["quarter"] = s["period"].map(month_to_quarter)
    s = s.sort_values("period")
    first_n = s.groupby("quarter").head(n_months)
    g = first_n.groupby("quarter").agg(value=("value", "sum"), n=("value", "size"))
    g = g[g.n == n_months]["value"].sort_index(key=lambda i: i.map(quarter_sort_key))
    return (g / g.shift(4) - 1).dropna()


def run() -> pd.DataFrame:
    cfg = load_config()
    z = cfg["settings"]["scenario_z"]
    monthly = pd.read_csv(DATA / "master_monthly.csv")
    quarterly = pd.read_csv(DATA / "master_quarterly.csv")
    qtd_path = DATA / "qtd.csv"
    ranking = safe_read(OUTPUT / "model_ranking.csv", required=True)

    if safe_read(qtd_path).empty:
        LOG.warning("no partial quarter in progress - nothing to nowcast")
        return pd.DataFrame()

    qtd = pd.read_csv(qtd_path)
    target_q = qtd["quarter"].iloc[0]
    guidance = load_guidance()

    tgt_hist = quarterly[quarterly.series_id == TARGET].set_index("quarter")
    prior_q = f"{int(target_q[:4]) - 1}Q{target_q[-1]}"
    if prior_q not in tgt_hist.index:
        raise RuntimeError(f"missing {prior_q} AMKR revenue - cannot convert YoY to a level")
    base_level = float(tgt_hist.loc[prior_q, "value"])
    y = tgt_hist["yoy"].dropna()

    rows = []
    # Take the best lead for each DISTINCT series, not the top 3 rows outright.
    # The raw ranking is often the same series at leads 0/1/2, and three rows of
    # one series agreeing is not corroboration - it is the same signal measured
    # three times, which reads as false confidence.
    best_per_series = (ranking.sort_values("backtest_rmse")
                              .groupby("series_id", as_index=False).first()
                              .sort_values("backtest_rmse"))
    for _, cand in best_per_series.head(3).iterrows():
        sid, lead = cand["series_id"], int(cand["lead_months"])
        # The predictor value for the target quarter MUST come from the same
        # lead-shifted window the model was fitted on. Taking it from qtd.csv
        # (which is never lead-shifted) calibrates on one window and predicts
        # from another - invisible at lead 0, materially wrong at lead > 0.
        n_months = months_available(monthly, sid, lead, target_q)
        if n_months == 0:
            continue
        x_hist = partial_quarter_yoy(monthly, sid, lead, n_months)
        if target_q not in x_hist.index:
            LOG.warning("%s: no lead-%d window for %s - skipping", sid, lead, target_q)
            continue
        x_now = float(x_hist.loc[target_q])

        # y has no value for target_q, so the inner join drops it from the fit.
        # No lookahead: the quarter being predicted never trains the model.
        joined = pd.concat([y.rename("y"), x_hist.rename("x")], axis=1).dropna()
        if len(joined) < 8:
            LOG.warning("%s: only %d matched quarters at n=%d months - skipping",
                        sid, len(joined), n_months)
            continue

        fit = sm.OLS(joined["y"].values, sm.add_constant(joined["x"].values)).fit()
        alpha, beta = float(fit.params[0]), float(fit.params[1])
        sigma = float(np.std(fit.resid, ddof=2))

        pred_yoy = alpha + beta * x_now
        model_level = base_level * (1 + pred_yoy)
        lo_model = base_level * (1 + pred_yoy - z * sigma)
        hi_model = base_level * (1 + pred_yoy + z * sigma)

        g = guidance[guidance.quarter == target_q] if not guidance.empty else pd.DataFrame()
        if not g.empty:
            g_lo = float(g["guide_low_usdm"].iloc[0])
            g_hi = float(g["guide_high_usdm"].iloc[0])
            g_mid = float(g["guide_mid"].iloc[0])
            bear, bull = min(g_lo, lo_model), max(g_hi, hi_model)
            anchor = "guidance-anchored"
        else:
            g_lo = g_hi = g_mid = np.nan
            bear, bull = lo_model, hi_model
            anchor = "model-only (no guidance on file)"

        rows.append({
            "quarter": target_q,
            "predictor": sid,
            "lead_months": lead,
            "months_reported": n_months,
            "matched_quarters_in_fit": len(joined),
            "predictor_qtd_yoy": x_now,
            "implied_amkr_yoy": pred_yoy,
            "prior_year_revenue_usdm": base_level,
            "bear_usdm": bear,
            "base_usdm": model_level,
            "bull_usdm": bull,
            "guide_low_usdm": g_lo,
            "guide_mid_usdm": g_mid,
            "guide_high_usdm": g_hi,
            "delta_vs_guide_mid_usdm": model_level - g_mid if g_mid == g_mid else np.nan,
            "delta_vs_guide_mid_pct": (model_level / g_mid - 1) if g_mid == g_mid else np.nan,
            "model_r2": fit.rsquared,
            "resid_sd_pp": sigma,
            "anchor": anchor,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        LOG.warning("no predictor produced a usable nowcast")
        return out

    OUTPUT.mkdir(exist_ok=True)
    out.to_csv(OUTPUT / "scenarios.csv", index=False)
    top = out.iloc[0]
    LOG.info("%s nowcast via %s (%d months in): base $%.0fm | bear $%.0fm | bull $%.0fm",
             target_q, top.predictor, top.months_reported,
             top.base_usdm, top.bear_usdm, top.bull_usdm)
    if top.guide_mid_usdm == top.guide_mid_usdm:
        LOG.info("vs guidance midpoint $%.0fm: %+.1f%%",
                 top.guide_mid_usdm, 100 * top.delta_vs_guide_mid_pct)
    return out


if __name__ == "__main__":
    run()
