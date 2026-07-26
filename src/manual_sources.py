"""Loaders for the two inputs that resist scraping.

SIA/WSTS publishes regional billings through a press release with no stable
machine-readable endpoint, and AMKR's guidance range lives in the prose of an
8-K. Both are therefore CSVs a human (or the monthly Claude refresh task)
appends to. Keeping them as plain CSV in git means every revision is a diff.

IMPORTANT on SIA: the monthly figures WSTS publishes are three-month moving
averages, not discrete months. They are smoothed, so they lag turning points
and their quarterly sums are not true quarterly billings. Treated here as a
coincident-to-lagging confirmation series, never as a leading indicator.
"""
from __future__ import annotations

import pandas as pd

from common import MANUAL, log, read_manual_csv, save_raw, tidy, today

LOG = log("manual")

SIA_FILE = MANUAL / "sia_billings.csv"
GUIDANCE_FILE = MANUAL / "amkr_guidance.csv"
SIA_COLS = ["SIA_GLOBAL", "SIA_AMERICAS", "SIA_EUROPE", "SIA_JAPAN", "SIA_CHINA", "SIA_APAC"]


def load_sia() -> pd.DataFrame:
    if not SIA_FILE.exists():
        LOG.warning("no SIA file at %s - SIA series will be absent", SIA_FILE)
        return tidy([])
    raw = read_manual_csv(SIA_FILE, SIA_COLS)
    # Drop the shipped example row and any placeholder the user left behind.
    raw = raw[~raw.get("notes", pd.Series(dtype=str))
              .astype(str).str.upper().str.startswith("EXAMPLE", na=False)]
    rows = []
    for _, r in raw.iterrows():
        for col in SIA_COLS:
            if col in raw.columns and pd.notna(r.get(col)):
                rows.append({
                    "period": str(r["period"]).strip(),
                    "series_id": col,
                    "value": float(r[col]),
                    "unit": "USD_MILLIONS",
                    "freq": "M",
                    "source": "SIA/WSTS (manual, 3mma)",
                    "retrieved": today(),
                })
    df = tidy(rows)
    if df.empty:
        raise RuntimeError(
            f"{SIA_FILE.name} has no usable rows - only the EXAMPLE row, which is "
            "ignored by design. Delete it and append real SIA/WSTS monthly "
            "billings, or the six SIA series stay absent from the model."
        )
    LOG.info("SIA: %d rows, %s to %s", len(df), df.period.min(), df.period.max())
    save_raw(df, "sia_billings")
    return df


def load_guidance() -> pd.DataFrame:
    if not GUIDANCE_FILE.exists():
        LOG.warning("no guidance file - scenarios will fall back to pure model bands")
        return pd.DataFrame(columns=["quarter", "guide_low_usdm", "guide_high_usdm", "guide_mid"])
    g = read_manual_csv(GUIDANCE_FILE,
                        ["guide_low_usdm", "guide_high_usdm"], period_col="quarter")
    g = g.dropna(subset=["guide_low_usdm", "guide_high_usdm"])
    g["guide_mid"] = (g["guide_low_usdm"] + g["guide_high_usdm"]) / 2
    LOG.info("guidance: %d quarters", len(g))
    return g


if __name__ == "__main__":
    load_sia()
    load_guidance()
