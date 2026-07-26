"""Advanced / Mainstream segment model, re-aggregated to a total.

WHAT THIS IS FOR
Amkor's product mix moved from ~47% Advanced (Q1 2019) to ~81% (Q1 2026). A
single beta fitted on total revenue averages across that drift. Fitting the two
product groups against the peers that actually serve them, then re-aggregating,
targets that mis-specification directly.

DISCIPLINE - the reason this is defensible with 23 quarters
Two rules, both enforced in code rather than left to good intentions:

1. The peer-to-segment mapping is PRE-SPECIFIED in config/series.yaml on
   economic grounds. It is not chosen by fit. Advanced gets ASE, KYEC and TSMC
   (advanced SiP, advanced/HPC test, leading-edge foundry); Mainstream gets PTI
   and ChipMOS (wirebond, memory, display driver).

2. The lead is FIXED AT ZERO. Amkor and the Taiwan OSATs are same-tier peers
   serving overlapping customers, so coincident is the economic prior. Other
   leads are computed and printed, but flagged diagnostic and never selected
   on. Searching 2 segments x 4 leads x peer subsets on 23 quarters would
   reliably produce a good-looking number and an unreliable one.

This model does not have more data than the aggregate model - the same 23
quarters, split two ways. It is better specified, not better powered. If it
does not beat the aggregate model out of sample, prefer the aggregate model.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm

from common import (DATA, OUTPUT, load_config, log, month_to_quarter,
                    next_quarter, prior_year_quarter, quarter_sort_key,
                    safe_read, shift_month)

LOG = log("segment_model")
PRESPECIFIED_LEAD = 0


# --------------------------------------------------------------------------
def peer_composite(monthly: pd.DataFrame, peers: list[str], cap: float,
                   method: str = "equal", min_warn: float = 0.10) -> pd.Series:
    """Blended index of the given peers, indexed by month.

    Each peer is normalised to its own base before weighting, so the weights
    control how much INFORMATION each contributes, not how much revenue. That
    is why 'equal' is the right default across mixed-scale peers: revenue
    weighting across a 70x scale gap hands the composite to the largest name
    regardless of relevance.
    """
    wide = monthly[monthly.series_id.isin(peers)].pivot_table(
        index="period", columns="series_id", values="value").sort_index()
    present = [p for p in peers if p in wide.columns]
    if len(present) < 1:
        return pd.Series(dtype=float)
    wide = wide[present].dropna()
    if wide.empty:
        return pd.Series(dtype=float)

    if method == "equal":
        w = pd.Series(1.0 / len(present), index=present)
    else:
        w = wide.tail(12).sum()
        w = w / w.sum()
    if method != "equal" and cap < 1.0 and len(present) > 1:
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
    LOG.info("  composite weights (%s): %s", method,
             {k: f"{v:.0%}" for k, v in w.items()})
    thin = w[w < min_warn]
    if len(thin):
        LOG.warning("  %s contribute <%.0f%% each - the composite is not really "
                    "using them", list(thin.index), 100 * min_warn)
    return (wide / wide.iloc[0] * 100).mul(w, axis=1).sum(axis=1)


def quarterly_yoy_from_monthly(s: pd.Series, lead: int, n_months: int = 3) -> pd.Series:
    f = pd.DataFrame({"period": s.index, "value": s.values})
    f["period"] = f["period"].map(lambda p: shift_month(p, lead))
    f["quarter"] = f["period"].map(month_to_quarter)
    f = f.sort_values("period")
    first_n = f.groupby("quarter").head(n_months)
    g = first_n.groupby("quarter").agg(value=("value", "sum"), n=("value", "size"))
    g = g[g.n == n_months]["value"].sort_index(key=lambda i: i.map(quarter_sort_key))
    return (g / g.shift(4) - 1).dropna()


def segment_yoy(seg: pd.DataFrame, sid: str) -> pd.Series:
    s = seg[seg.series_id == sid].set_index("period")["value"]
    s = s.sort_index(key=lambda i: i.map(quarter_sort_key))
    return (s / s.shift(4) - 1).dropna()


# --------------------------------------------------------------------------
def mix_decomposition(seg: pd.DataFrame) -> pd.DataFrame:
    """Split total YoY growth into each segment's contribution.

    With prior-year shares as weights this is exact, not an approximation:
        total growth = sum over segments of (prior-year share x segment growth)
    It answers 'how much of the change is Advanced doing the work' - the thing
    a single aggregate beta cannot see.
    """
    wide = seg.pivot_table(index="period", columns="series_id", values="value")
    wide = wide.sort_index(key=lambda i: i.map(quarter_sort_key))
    if wide.shape[1] < 2:
        return pd.DataFrame()
    wide["TOTAL"] = wide.sum(axis=1)
    out = []
    for q in wide.index:
        pq = prior_year_quarter(q)
        if pq not in wide.index:
            continue
        row = {"quarter": q, "total_yoy": wide.loc[q, "TOTAL"] / wide.loc[pq, "TOTAL"] - 1}
        for sid in [c for c in wide.columns if c != "TOTAL"]:
            share_prior = wide.loc[pq, sid] / wide.loc[pq, "TOTAL"]
            growth = wide.loc[q, sid] / wide.loc[pq, sid] - 1
            row[f"{sid}_share"] = wide.loc[q, sid] / wide.loc[q, "TOTAL"]
            row[f"{sid}_yoy"] = growth
            row[f"{sid}_contrib"] = share_prior * growth
        out.append(row)
    return pd.DataFrame(out)


# --------------------------------------------------------------------------
def run() -> pd.DataFrame:
    cfg = load_config()
    scfg = cfg.get("segments", {})
    if not scfg.get("enabled"):
        raise RuntimeError("segments disabled in config")

    monthly = safe_read(DATA / "master_monthly.csv", required=True)
    seg = safe_read(DATA / "raw" / "amkr_segments.csv")
    if seg.empty:
        raise RuntimeError("no segment data - run fetch_amkr_segments first")
    seg = seg.rename(columns={"period": "period"})

    members = scfg["members"]
    if scfg.get("pti_to_advanced"):
        for m in members:
            if m["series_id"] == "AMKR_ADVANCED" and "PTI" not in m["peers"]:
                m["peers"] = m["peers"] + ["PTI"]
            if m["series_id"] == "AMKR_MAINSTREAM":
                m["peers"] = [p for p in m["peers"] if p != "PTI"]
        LOG.warning("SENSITIVITY MODE: PTI mapped to Advanced")

    # nowcast target = quarter after the last one with segment data
    have_q = sorted(seg.period.unique(), key=quarter_sort_key)
    target_q = next_quarter(have_q[-1])
    prior_q = prior_year_quarter(target_q)
    LOG.info("segment nowcast target %s (segment data through %s)", target_q, have_q[-1])

    cap = float(scfg.get("max_weight", 1.0))
    min_q = cfg["settings"]["min_quarters_for_fit"]
    rows, seg_levels = [], {}

    for m in members:
        sid, peers = m["series_id"], m["peers"]
        LOG.info("%s <- %s", sid, peers)
        comp = peer_composite(monthly, peers, cap,
                              method=scfg.get("weighting", "equal"),
                              min_warn=float(scfg.get("min_weight_warn", 0.10)))
        if comp.empty:
            LOG.warning("  no peer data for %s - skipping", sid)
            continue

        y = segment_yoy(seg, sid)
        x = quarterly_yoy_from_monthly(comp, PRESPECIFIED_LEAD)
        joined = pd.concat([y.rename("y"), x.rename("x")], axis=1).dropna()
        if len(joined) < min_q:
            LOG.warning("  only %d matched quarters for %s (need %d) - skipping",
                        len(joined), sid, min_q)
            continue

        fit = sm.OLS(joined["y"].values, sm.add_constant(joined["x"].values)).fit()
        alpha, beta = float(fit.params[0]), float(fit.params[1])
        sigma = float(np.std(fit.resid, ddof=2))

        if target_q not in x.index:
            LOG.warning("  no peer window for %s at %s - skipping", sid, target_q)
            continue
        x_now = float(x.loc[target_q])
        pred_yoy = alpha + beta * x_now

        base_seg = seg[(seg.series_id == sid) & (seg.period == prior_q)]
        if base_seg.empty:
            LOG.warning("  missing %s for %s - cannot convert to a level", prior_q, sid)
            continue
        prior_level = float(base_seg["value"].iloc[0])
        level = prior_level * (1 + pred_yoy)
        seg_levels[sid] = (level, prior_level * sigma)

        # Diagnostic only. Printed so a wildly different lead is visible, but
        # PRESPECIFIED_LEAD is what the forecast uses - no selection on these.
        diag = {}
        for lead in (1, 2, 3):
            xl = quarterly_yoy_from_monthly(comp, lead)
            j2 = pd.concat([y.rename("y"), xl.rename("x")], axis=1).dropna()
            if len(j2) >= min_q:
                diag[f"corr_lead{lead}"] = float(j2["x"].corr(j2["y"]))

        rows.append({
            "quarter": target_q, "segment": sid, "peers": "+".join(peers),
            "lead_months": PRESPECIFIED_LEAD, "n_obs": len(joined),
            "peer_yoy": x_now, "implied_segment_yoy": pred_yoy,
            "prior_year_usdm": prior_level, "predicted_usdm": level,
            "resid_sd_pp": sigma, "in_sample_r2": fit.rsquared,
            "corr_lead0": float(joined["x"].corr(joined["y"])), **diag,
        })

    if not rows:
        raise RuntimeError(
            "no segment fitted - usually too few quarters of segment history. "
            "The filing parser needs to succeed, or fill data/manual/amkr_segments.csv."
        )

    out = pd.DataFrame(rows)
    OUTPUT.mkdir(exist_ok=True)
    out.to_csv(OUTPUT / "segment_models.csv", index=False)

    mix = mix_decomposition(seg)
    if not mix.empty:
        mix.to_csv(OUTPUT / "mix_decomposition.csv", index=False)
        first, last = mix.iloc[0], mix.iloc[-1]
        for sid in [m["series_id"] for m in members]:
            col = f"{sid}_share"
            if col in mix.columns:
                LOG.info("%s share: %.1f%% (%s) -> %.1f%% (%s)", sid,
                         100 * first[col], first.quarter, 100 * last[col], last.quarter)

    # ---- re-aggregate to a total -----------------------------------------
    if len(seg_levels) == len(members):
        total = sum(v[0] for v in seg_levels.values())
        # Segment errors are positively correlated (shared cycle), so adding
        # them in quadrature would understate the band. Sum them instead -
        # the conservative choice, and the honest one at this sample size.
        total_sd = sum(v[1] for v in seg_levels.values())
        summary = pd.DataFrame([{
            "quarter": target_q, "method": "segment sum",
            **{f"{k}_usdm": round(v[0], 1) for k, v in seg_levels.items()},
            "total_usdm": round(total, 1), "total_sd_usdm": round(total_sd, 1),
        }])
        summary.to_csv(OUTPUT / "segment_total.csv", index=False)
        LOG.info("segment-sum total for %s: $%.0fm (+/-$%.0fm)", target_q, total, total_sd)

        agg = safe_read(OUTPUT / "scenarios.csv")
        if not agg.empty:
            a = float(agg["base_usdm"].iloc[0])
            LOG.info("aggregate model said $%.0fm; segment sum differs by $%.0fm (%+.1f%%)",
                     a, total - a, 100 * (total / a - 1))
            # A disagreement only carries information if the segment model is at
            # least as precise. If its band is wider, the gap is noise and the
            # aggregate model should be preferred - say so rather than letting
            # the reader treat divergence as insight.
            #
            # Derive the aggregate SD from resid_sd_pp directly, NOT by backing
            # it out of the bear/bull bounds. Those are guidance-anchored: bear
            # is min(guidance low, model low) and bull is max(guidance high,
            # model high), so whichever side guidance is binding on carries no
            # information about model dispersion. Reading dispersion off a
            # clamped bound overstated aggregate uncertainty by ~40% in the
            # first live run, which flattered the segment model.
            agg_sd = (float(agg["resid_sd_pp"].iloc[0])
                      * float(agg["prior_year_revenue_usdm"].iloc[0]))
            verdict = ("segment model is MORE precise - the gap is worth reading"
                       if total_sd < agg_sd else
                       "segment model is LESS precise than the aggregate - "
                       "treat the gap as noise and prefer the aggregate forecast")
            LOG.warning("precision: segment +/-$%.0fm vs aggregate +/-$%.0fm -> %s",
                        total_sd, agg_sd, verdict)
            summary["aggregate_usdm"] = round(a, 1)
            summary["aggregate_sd_usdm"] = round(agg_sd, 1)
            summary["verdict"] = verdict
            summary.to_csv(OUTPUT / "segment_total.csv", index=False)
    return out


if __name__ == "__main__":
    run()
