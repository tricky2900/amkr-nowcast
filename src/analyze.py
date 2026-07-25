"""Lead/lag screening and the nowcast regression.

Everything here works in YEAR-OVER-YEAR SPACE, deliberately. Correlating raw
revenue levels across these series produces r > 0.95 for almost any pair,
because they all trend with the semiconductor cycle and with inflation. That
number is meaningless - it measures shared trend, not shared information. YoY
differencing strips the trend and leaves the cyclical signal that actually
carries forecasting content.

Two things this module deliberately does NOT do:

* It does not throw every series into one regression. The predictors are
  strongly collinear with each other (they are all reading the same cycle), so
  a kitchen-sink fit yields unstable, sign-flipping coefficients that backtest
  far worse than a single well-chosen input. Candidates are screened
  univariately and at most two are combined.
* It does not report in-sample R-squared as if it were forecast accuracy. The
  honest number is from a rolling-origin backtest, refitting at each step on
  only the data that existed at the time. That number is usually much worse
  than in-sample fit, and it is the one that drives the scenario bands.

Sample-size warning: 5 years of history is 20 quarters. That is a small sample
for a regression, and it spans an unusually violent cycle. Treat coefficients
as indicative, widen the bands rather than narrowing them, and extend
history_years in config if you want more degrees of freedom.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm

from common import (DATA, OUTPUT, load_config, log, month_to_quarter,
                    quarter_sort_key, shift_month)

LOG = log("analyze")
TARGET = "AMKR"


# --------------------------------------------------------------------------
def _quarterly_yoy_at_lead(monthly: pd.DataFrame, sid: str, lead: int) -> pd.Series:
    """Quarterly YoY for one monthly series, shifted `lead` months forward.

    lead=1 means 'this month's peer revenue is being lined up against AMKR one
    month later', i.e. we are testing the peer as a one-month leading indicator.
    """
    s = monthly[monthly.series_id == sid].copy()
    if s.empty:
        return pd.Series(dtype=float)
    s["period"] = s["period"].map(lambda p: shift_month(p, lead))
    s["quarter"] = s["period"].map(month_to_quarter)
    g = s.groupby("quarter").agg(value=("value", "sum"), n=("value", "size"))
    g = g[g.n == 3]["value"]
    g = g.sort_index(key=lambda i: i.map(quarter_sort_key))
    return (g / g.shift(4) - 1).dropna()


def lead_lag_table(monthly: pd.DataFrame, target_yoy: pd.Series, max_lead: int) -> pd.DataFrame:
    rows = []
    for sid in sorted(monthly.series_id.unique()):
        for lead in range(0, max_lead + 1):
            x = _quarterly_yoy_at_lead(monthly, sid, lead)
            joined = pd.concat([target_yoy.rename("y"), x.rename("x")], axis=1).dropna()
            if len(joined) < 6:
                continue
            r = joined["x"].corr(joined["y"])
            rows.append({
                "series_id": sid, "lead_months": lead, "n_obs": len(joined),
                "corr": r, "r_squared": r ** 2,
            })
    tbl = pd.DataFrame(rows)
    if tbl.empty:
        return tbl
    best = (tbl.sort_values("corr", ascending=False)
               .groupby("series_id", as_index=False).first()
               .sort_values("corr", ascending=False))
    best["interpretation"] = best["lead_months"].map({
        0: "coincident", 1: "leads ~1 month", 2: "leads ~2 months", 3: "leads ~1 quarter"
    })
    return best


# --------------------------------------------------------------------------
def backtest(y: pd.Series, x: pd.Series, min_train: int) -> dict:
    """Rolling-origin one-step-ahead backtest. Refits from scratch at each
    origin so no future information leaks into an earlier prediction."""
    idx = sorted(set(y.index) & set(x.index), key=quarter_sort_key)
    errs, preds = [], []
    for i in range(min_train, len(idx)):
        train = idx[:i]
        test = idx[i]
        X = sm.add_constant(x.loc[train].values)
        model = sm.OLS(y.loc[train].values, X).fit()
        pred = float(model.params[0] + model.params[1] * x.loc[test])
        errs.append(y.loc[test] - pred)
        preds.append({"quarter": test, "actual": y.loc[test], "predicted": pred})
    if not errs:
        return {"n": 0, "rmse": np.nan, "mae": np.nan, "preds": pd.DataFrame()}
    e = np.array(errs)
    return {
        "n": len(e),
        "rmse": float(np.sqrt((e ** 2).mean())),
        "mae": float(np.abs(e).mean()),
        "bias": float(e.mean()),
        "preds": pd.DataFrame(preds),
    }


def fit_nowcast(quarterly: pd.DataFrame, monthly: pd.DataFrame, cfg: dict) -> dict:
    """Pick the best single predictor by backtest RMSE, not by in-sample fit."""
    tgt = quarterly[quarterly.series_id == TARGET].set_index("quarter")["yoy"].dropna()
    if len(tgt) < cfg["settings"]["min_quarters_for_fit"]:
        raise RuntimeError(
            f"only {len(tgt)} quarters of AMKR YoY - need at least "
            f"{cfg['settings']['min_quarters_for_fit']}. Increase history_years."
        )

    max_lead = cfg["settings"]["max_lead_months"]
    candidates = [s for s in monthly.series_id.unique()]
    results = []
    min_train = max(8, cfg["settings"]["min_quarters_for_fit"] - 4)

    for sid in candidates:
        for lead in range(0, max_lead + 1):
            x = _quarterly_yoy_at_lead(monthly, sid, lead)
            joined = pd.concat([tgt.rename("y"), x.rename("x")], axis=1).dropna()
            if len(joined) < min_train + 2:
                continue
            bt = backtest(joined["y"], joined["x"], min_train)
            if bt["n"] == 0:
                continue
            X = sm.add_constant(joined["x"].values)
            fit = sm.OLS(joined["y"].values, X).fit()
            results.append({
                "series_id": sid, "lead_months": lead, "n_obs": len(joined),
                "in_sample_r2": fit.rsquared, "backtest_rmse": bt["rmse"],
                "backtest_mae": bt["mae"], "backtest_bias": bt["bias"],
                "alpha": float(fit.params[0]), "beta": float(fit.params[1]),
                "resid_sd": float(np.std(fit.resid, ddof=2)),
                "_bt": bt,
            })

    if not results:
        raise RuntimeError("no predictor had enough overlapping history to fit")

    ranked = pd.DataFrame(results).sort_values("backtest_rmse")
    best = ranked.iloc[0]
    LOG.info("best predictor: %s at lead %d | backtest RMSE %.1f pp | in-sample R2 %.2f",
             best.series_id, best.lead_months, best.backtest_rmse * 100, best.in_sample_r2)

    OUTPUT.mkdir(exist_ok=True)
    ranked.drop(columns=["_bt"]).to_csv(OUTPUT / "model_ranking.csv", index=False)
    best["_bt"]["preds"].to_csv(OUTPUT / "backtest_predictions.csv", index=False)

    return {
        "predictor": best.series_id,
        "lead_months": int(best.lead_months),
        "alpha": best.alpha,
        "beta": best.beta,
        # Backtest RMSE is the honest dispersion; residual SD flatters the model.
        "sigma": float(max(best.backtest_rmse, best.resid_sd)),
        "in_sample_r2": best.in_sample_r2,
        "backtest_rmse": best.backtest_rmse,
        "n_obs": int(best.n_obs),
        "ranking": ranked.drop(columns=["_bt"]),
    }


def run() -> dict:
    cfg = load_config()
    monthly = pd.read_csv(DATA / "master_monthly.csv")
    quarterly = pd.read_csv(DATA / "master_quarterly.csv")
    tgt = quarterly[quarterly.series_id == TARGET].set_index("quarter")["yoy"].dropna()

    OUTPUT.mkdir(exist_ok=True)
    ll = lead_lag_table(monthly, tgt, cfg["settings"]["max_lead_months"])
    ll.to_csv(OUTPUT / "lead_lag.csv", index=False)
    LOG.info("lead/lag table written (%d series)", len(ll))

    model = fit_nowcast(quarterly, monthly, cfg)
    return {"lead_lag": ll, "model": model}


if __name__ == "__main__":
    run()
