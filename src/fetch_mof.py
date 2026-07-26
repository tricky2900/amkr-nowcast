"""Taiwan export statistics by commodity category (Ministry of Finance).

CAVEAT - read before trusting this module. Of the four sources in this repo
this is the one most likely to need adjustment on first run. MOF publishes
through an interactive portal (portal.sw.nat.gov.tw) rather than a documented
REST API, and the category codes in its commodity tree are not stable across
site revisions. The code below targets the portal's JSON query endpoint and
falls back to a manual CSV when that fails, rather than pretending success.

Release timing: roughly the 8th of the following month, so ahead of the
company filings on the 10th.

If the API path breaks, two options, in order of effort:
  1. Export the monthly series once from the portal UI into
     data/manual/taiwan_exports.csv (same shape as the SIA file) - the
     pipeline reads it transparently and nothing downstream changes.
  2. Re-point CATEGORY_CODES below after inspecting a live portal response.
"""
from __future__ import annotations

import pandas as pd
import requests

from common import (MANUAL, load_config, log, read_manual_csv, save_raw,
                    tidy, today, validate)

LOG = log("fetch_mof")
UA = {"User-Agent": "amkr-nowcast/1.0 (research; contact via repo issues)"}

MOF_ENDPOINT = "https://portal.sw.nat.gov.tw/APGA/api/GA03/getData"
FALLBACK = MANUAL / "taiwan_exports.csv"

# MOF commodity chapter groupings -> our series ids.
# Verify against a live response before relying on these.
CATEGORY_CODES = {
    "TWEX_TOTAL":      "TOTAL",
    "TWEX_MACH_ELEC":  "16",     # Machinery & electrical equipment (HS section XVI)
    "TWEX_ELEC_PARTS": "8542",   # Electronic integrated circuits / parts
    "TWEX_MACHINERY":  "84",     # Machinery & mechanical appliances
    "TWEX_ELEC_MACH":  "85",     # Electrical machinery
    "TWEX_ICT_AV":     "ICT",    # ICT & audio-video products
    "TWEX_APPLIANCES": "APPL",   # Domestic appliances
}

DERIVED = {"TWEX_ELEC_ICT": ["TWEX_ELEC_PARTS", "TWEX_ICT_AV"]}


def from_api(start_year: int) -> pd.DataFrame:
    rows = []
    for series_id, code in CATEGORY_CODES.items():
        payload = {"type": "export", "category": code, "startYear": start_year, "freq": "M"}
        r = requests.post(MOF_ENDPOINT, json=payload, headers=UA, timeout=60)
        r.raise_for_status()
        for rec in r.json().get("data", []):
            rows.append({
                "period": f"{int(rec['year'])}-{int(rec['month']):02d}",
                "series_id": series_id,
                "value": float(rec["valueUsd"]) / 1_000_000.0,
                "unit": "USD_MILLIONS",
                "freq": "M",
                "source": "MOF trade statistics portal",
                "retrieved": today(),
            })
    return tidy(rows)


def from_fallback() -> pd.DataFrame:
    if not FALLBACK.exists():
        LOG.error("no API and no fallback CSV at %s - export series will be missing", FALLBACK)
        return tidy([])
    raw = read_manual_csv(FALLBACK,
                          [c for c in CATEGORY_CODES] + ["TWEX_ELEC_ICT"])
    raw = raw[~raw.get("notes", pd.Series(dtype=str))
              .astype(str).str.upper().str.startswith("EXAMPLE", na=False)]
    value_cols = [c for c in raw.columns if c.startswith("TWEX_")]
    rows = []
    for _, r in raw.iterrows():
        for col in value_cols:
            if pd.notna(r.get(col)):
                rows.append({
                    "period": str(r["period"]).strip(),
                    "series_id": col,
                    "value": float(r[col]),
                    "unit": "USD_MILLIONS",
                    "freq": "M",
                    "source": "MOF (manual export)",
                    "retrieved": today(),
                })
    LOG.info("loaded %d export rows from manual fallback", len(rows))
    return tidy(rows)


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Electronic + ICT is a sum of two published categories, not its own line."""
    if df.empty:
        return df
    wide = df.pivot_table(index="period", columns="series_id", values="value")
    extra = []
    for new_id, parts in DERIVED.items():
        if all(p in wide.columns for p in parts):
            s = wide[parts].sum(axis=1, min_count=len(parts)).dropna()
            for period, val in s.items():
                extra.append({
                    "period": period, "series_id": new_id, "value": float(val),
                    "unit": "USD_MILLIONS", "freq": "M",
                    "source": f"derived: {' + '.join(parts)}", "retrieved": today(),
                })
        else:
            LOG.warning("cannot derive %s - missing components", new_id)
    return pd.concat([df, tidy(extra)], ignore_index=True) if extra else df


def run() -> pd.DataFrame:
    cfg = load_config()
    start_year = pd.Timestamp.today().year - cfg["settings"]["history_years"]
    try:
        df = from_api(start_year)
        LOG.info("MOF API returned %d rows", len(df))
    except Exception as exc:
        LOG.error("MOF API failed (%s) - falling back to manual CSV", exc)
        df = from_fallback()

    df = add_derived(df)
    expected = [s["series_id"] for s in cfg["taiwan_exports"]]
    for w in validate(df, "mof", expected):
        LOG.warning(w)
    if df.empty:
        # Returning nothing is a failure, even though nothing threw. Raise so
        # run_all grades this DEGRADED rather than reporting a clean run.
        raise RuntimeError(
            "no export rows from the MOF API or the manual fallback. Fill "
            "data/manual/taiwan_exports.csv from the portal's CSV export, or "
            "repoint CATEGORY_CODES after inspecting a live response."
        )
    save_raw(df, "taiwan_exports")
    return df


if __name__ == "__main__":
    run()
