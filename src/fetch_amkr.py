"""AMKR quarterly net sales from SEC EDGAR's XBRL company-concept API.

Two wrinkles worth knowing:

* Companies file Q1-Q3 as ~91-day durations in 10-Qs, but Q4 is never filed on
  its own. It has to be derived: Q4 = FY - (Q1 + Q2 + Q3). We do that here so
  the target series has no annual gap every December.
* The same fact gets re-filed in later periods (comparatives). We keep the
  earliest-filed version of each period so a restatement does not silently
  rewrite history without showing up as a diff in git.
"""
from __future__ import annotations

import os
import re
import time

import pandas as pd
import requests

from common import load_config, log, save_raw, tidy, today, validate

LOG = log("fetch_amkr")

CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"

# SEC fair-access policy: every request must carry a User-Agent naming the app
# AND a contact email. Without an email it is a hard 403, and a 403 blocks the
# whole IP for about ten minutes - so we refuse to send the request at all
# rather than burn the runner's IP on a call we know will be rejected.
# Rate limit is 10 req/sec; we stay well under.
UA_ENV = "SEC_USER_AGENT"
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _headers() -> dict:
    ua = os.environ.get(UA_ENV, "").strip()
    if not ua or not EMAIL_RE.search(ua):
        raise RuntimeError(
            f"SEC requires a User-Agent containing a contact email, and {UA_ENV} "
            f"is {'not set' if not ua else 'missing an email address'}.\n"
            "  Local:  export SEC_USER_AGENT='amkr-nowcast you@example.com'\n"
            "  GitHub: repo Settings > Secrets and variables > Actions > Variables\n"
            "          > New variable, name SEC_USER_AGENT, value as above.\n"
            "Any address you can be reached at is fine; the SEC only uses it to "
            "contact you if your requests cause problems."
        )
    return {
        "User-Agent": ua,
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }

# AMKR has used different revenue tags across the years; try in order.
TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]


def _get(url: str, headers: dict):
    for attempt in range(4):
        r = requests.get(url, headers=headers, timeout=45)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None                      # tag simply not used by this filer
        if r.status_code == 403:
            raise RuntimeError(
                "SEC returned 403 Forbidden. Either SEC_USER_AGENT is not being "
                "honoured, or this IP is in a ~10 minute block from an earlier "
                "rejected request. Wait 10 minutes and retry."
            )
        LOG.warning("%s -> HTTP %s", url, r.status_code)
        time.sleep(2 ** attempt)
    return None


def _facts(cik: str) -> pd.DataFrame:
    headers = _headers()
    frames = []
    for tag in TAGS:
        time.sleep(0.15)                     # stay well inside 10 req/sec
        payload = _get(CONCEPT.format(cik=cik, tag=tag), headers)
        if not payload:
            continue
        recs = payload.get("units", {}).get("USD", [])
        if not recs:
            continue
        f = pd.DataFrame(recs)
        f["tag"] = tag
        frames.append(f)
        LOG.info("tag %s -> %d facts", tag, len(f))
    if not frames:
        raise RuntimeError("no revenue facts returned by EDGAR for this CIK")
    df = pd.concat(frames, ignore_index=True)
    df["start"] = pd.to_datetime(df["start"])
    df["end"] = pd.to_datetime(df["end"])
    df["filed"] = pd.to_datetime(df["filed"])
    df["days"] = (df["end"] - df["start"]).dt.days
    return df


def run() -> pd.DataFrame:
    cfg = load_config()
    cik = cfg["target"]["cik"]
    years = cfg["settings"]["history_years"]
    cutoff = pd.Timestamp.today() - pd.DateOffset(years=years + 1)

    df = _facts(cik)

    # --- quarterly facts (Q1-Q3) -----------------------------------------
    q = df[(df.days.between(80, 100)) & (df.end >= cutoff)].copy()
    q["quarter"] = q["end"].dt.year.astype(str) + "Q" + q["end"].dt.quarter.astype(str)
    q = q.sort_values("filed").drop_duplicates("quarter", keep="first")

    # --- annual facts, used only to back out Q4 ---------------------------
    a = df[(df.days.between(350, 380)) & (df.end >= cutoff)].copy()
    a["year"] = a["end"].dt.year
    a = a.sort_values("filed").drop_duplicates("year", keep="first")

    have = dict(zip(q["quarter"], q["val"]))
    for _, row in a.iterrows():
        y = int(row["year"])
        q4 = f"{y}Q4"
        first3 = [have.get(f"{y}Q{i}") for i in (1, 2, 3)]
        if q4 not in have and all(v is not None for v in first3):
            have[q4] = float(row["val"]) - sum(first3)
            LOG.info("derived %s = FY%s - Q1..Q3", q4, y)

    rows = [{
        "period": k,
        "series_id": "AMKR",
        "value": v / 1_000_000.0,          # USD -> USD millions
        "unit": "USD_MILLIONS",
        "freq": "Q",
        "source": "SEC:XBRL companyconcept",
        "retrieved": today(),
    } for k, v in sorted(have.items())]

    out = tidy(rows)
    for w in validate(out, "amkr", ["AMKR"]):
        LOG.warning(w)
    save_raw(out, "amkr_quarterly")
    LOG.info("AMKR: %d quarters, %s to %s",
             len(out), out.period.iloc[0], out.period.iloc[-1])
    return out


if __name__ == "__main__":
    run()
