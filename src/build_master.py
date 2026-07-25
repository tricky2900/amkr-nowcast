"""Consolidate every raw source into the two tables everything downstream reads.

Outputs
  data/master_monthly.csv     tidy long, one row per period/series
  data/master_quarterly.csv   calendar-quarter aggregates + YoY, complete quarters only
  data/master_wide.csv        pivoted monthly levels, convenient for Excel/Power Query
  data/qtd.csv                current incomplete quarter, months-to-date (the nowcast input)

Design note: the OSAT composite is revenue-weighted rather than equal-weighted.
Equal weighting would let ChipMOS - roughly an order of magnitude smaller than
ASE - swing the composite as much as ASE does. Weights come from trailing
12-month revenue and are recomputed on each build, so they drift with the
industry instead of being frozen at whatever the mix was on day one.
"""
from __future__ import annotations

import pandas as pd

from common import (COLUMNS, DATA, load_config, log, month_to_quarter,
                    next_quarter, prior_year_quarter, quarter_sort_key,
                    read_raw, today)

LOG = log("build_master")

TARGET = "AMKR"
SOURCES = ["taiwan_companies", "taiwan_exports", "sia_billings", "amkr_quarterly"]


def _osat_composite(monthly: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    members = cfg["osat_composite"]["members"]
    wide = monthly[monthly.series_id.isin(members)].pivot_table(
        index="period", columns="series_id", values="value"
    )
    present = [m for m in members if m in wide.columns]
    if len(present) < 2:
        LOG.warning("OSAT composite needs >=2 members, have %s - skipping", present)
        return pd.DataFrame(columns=COLUMNS)

    wide = wide[present].sort_index()
    # Only months where every member has reported, so the composite never dips
    # merely because one company has not filed yet.
    wide = wide.dropna()
    if wide.empty:
        return pd.DataFrame(columns=COLUMNS)

    if cfg["osat_composite"].get("static_weights"):
        w = pd.Series(1 / len(present), index=present)
    else:
        w = wide.tail(12).sum()
        w = w / w.sum()
        cap = float(cfg["osat_composite"].get("max_weight", 1.0))
        if cap < 1.0:
            # Iteratively clip and redistribute onto the uncapped members.
            for _ in range(20):
                over = w[w > cap]
                if over.empty:
                    break
                excess = float((over - cap).sum())
                w[over.index] = cap
                free = w.index.difference(over.index)
                if len(free) == 0 or w[free].sum() == 0:
                    break
                w[free] += excess * w[free] / w[free].sum()
            w = w / w.sum()
    LOG.info("OSAT weights: %s", {k: f"{v:.1%}" for k, v in w.items()})

    # Weighted index: each member normalised to its own base, then combined.
    base = wide.iloc[0]
    idx = (wide / base * 100).mul(w, axis=1).sum(axis=1)
    return pd.DataFrame([{
        "period": p, "series_id": "OSAT_COMPOSITE", "value": float(v),
        "unit": "INDEX_BASE100", "freq": "M",
        "source": f"derived: revenue-weighted {'+'.join(present)}", "retrieved": today(),
    } for p, v in idx.items()])


def run() -> dict[str, pd.DataFrame]:
    cfg = load_config()
    frames = [read_raw(s) for s in SOURCES]
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise RuntimeError("no raw data found - run the fetchers first")
    allrows = pd.concat(frames, ignore_index=True)

    monthly = allrows[allrows.freq == "M"].copy()
    comp = _osat_composite(monthly, cfg)
    if not comp.empty:
        monthly = pd.concat([monthly, comp], ignore_index=True)

    monthly = monthly.drop_duplicates(["period", "series_id"], keep="last")
    monthly = monthly.sort_values(["series_id", "period"])
    monthly.to_csv(DATA / "master_monthly.csv", index=False)

    # ---- quarterly -------------------------------------------------------
    monthly["quarter"] = monthly["period"].map(month_to_quarter)
    agg = (monthly.groupby(["series_id", "quarter"])
                  .agg(value=("value", "sum"), n_months=("value", "size"))
                  .reset_index())
    complete = agg[agg.n_months == 3].copy()

    q_target = allrows[(allrows.freq == "Q")].copy()
    if not q_target.empty:
        q_target = q_target.rename(columns={"period": "quarter"})
        complete = pd.concat([
            complete,
            q_target.assign(n_months=3)[["series_id", "quarter", "value", "n_months"]],
        ], ignore_index=True)

    complete = complete.sort_values(
        ["series_id", "quarter"], key=lambda c: c.map(quarter_sort_key) if c.name == "quarter" else c
    )
    complete["yoy"] = complete.groupby("series_id")["value"].pct_change(4)
    complete["qoq"] = complete.groupby("series_id")["value"].pct_change(1)
    complete.to_csv(DATA / "master_quarterly.csv", index=False)

    # ---- the quarter being nowcast --------------------------------------
    # This is NOT simply "the incomplete quarter". Taiwan files monthly revenue
    # by the 10th while AMKR reports a quarter roughly four weeks after it ends,
    # so there is normally a window where the peer quarter is COMPLETE and AMKR's
    # is still unreported - which is exactly the most valuable moment to forecast.
    # The target is therefore the quarter following AMKR's last reported one,
    # with however many peer months exist (1, 2 or 3).
    amkr = complete[complete.series_id == TARGET]
    if amkr.empty:
        LOG.warning("no AMKR history - cannot identify a nowcast target")
    else:
        last_reported = max(amkr.quarter, key=quarter_sort_key)
        target_q = next_quarter(last_reported)
        prior_year = prior_year_quarter(target_q)
        pm = monthly[monthly.quarter.isin([target_q, prior_year])]
        rows = []
        for sid in sorted(pm.series_id.unique()):
            cur = pm[(pm.series_id == sid) & (pm.quarter == target_q)].sort_values("period")
            n = len(cur)
            if n == 0:
                continue
            pri = (pm[(pm.series_id == sid) & (pm.quarter == prior_year)]
                   .sort_values("period").head(n))
            if len(pri) == n and pri.value.sum():
                rows.append({
                    "quarter": target_q, "series_id": sid, "months_reported": n,
                    "qtd_value": cur.value.sum(),
                    "qtd_yoy": cur.value.sum() / pri.value.sum() - 1,
                })
        qtd = pd.DataFrame(rows)
        qtd.to_csv(DATA / "qtd.csv", index=False)
        LOG.info("nowcast target %s (AMKR last reported %s); %d series, %s months of peer data",
                 target_q, last_reported, len(qtd),
                 sorted(qtd.months_reported.unique()) if not qtd.empty else "no")

    # ---- wide view for Excel --------------------------------------------
    wide = monthly.pivot_table(index="period", columns="series_id", values="value")
    wide.sort_index().to_csv(DATA / "master_wide.csv")

    LOG.info("master built: %d monthly rows, %d complete quarters",
             len(monthly), complete.quarter.nunique())
    return {"monthly": monthly, "quarterly": complete}


if __name__ == "__main__":
    run()
