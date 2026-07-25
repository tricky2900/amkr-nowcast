"""Monthly revenue for the five Taiwan-listed comps.

Three sources, tried in order, because no single one does both history and
freshness well:

  1. FinMind  - free JSON API, full history, one call per ticker. Primary.
  2. TWSE OpenAPI t187ap05_L - authoritative but a ROLLING SNAPSHOT: it serves
     only the most recent filing month, so it cannot backfill. Used to confirm
     or repair the newest month.
  3. MOPS dated archive (t21sc03) - HTML, one page per year-month, all issuers.
     Slow but authoritative; the escape hatch if FinMind is down or wrong.

Taiwan issuers must file monthly revenue by the 10th of the following month,
so a refresh scheduled on the 12th will normally find a complete month.
"""
from __future__ import annotations

import io
import time

import pandas as pd
import requests

from common import (RAW, load_config, log, save_raw, tidy, today, validate)

LOG = log("fetch_twse")
UA = {"User-Agent": "amkr-nowcast/1.0 (research; contact via repo issues)"}

FINMIND = "https://api.finmindtrade.com/api/v4/data"
TWSE_OPENAPI = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
MOPS = "https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_{roc}_{month}_0.html"

# FinMind reports monthly revenue in TWD. We store thousands to keep the
# magnitudes readable and consistent with MOPS, which files in thousands.
UNIT = "TWD_THOUSANDS"


def _get(url: str, **kw) -> requests.Response:
    for attempt in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=45, **kw)
            if r.status_code == 200:
                return r
            LOG.warning("%s -> HTTP %s (attempt %s)", url, r.status_code, attempt + 1)
        except requests.RequestException as exc:
            LOG.warning("%s -> %s (attempt %s)", url, exc, attempt + 1)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"giving up on {url}")


def from_finmind(ticker: str, start: str) -> list[dict]:
    params = {
        "dataset": "TaiwanStockMonthRevenue",
        "data_id": ticker,
        "start_date": start,
    }
    payload = _get(FINMIND, params=params).json()
    if payload.get("status") != 200 or not payload.get("data"):
        raise RuntimeError(f"FinMind returned no data for {ticker}: {payload.get('msg')}")
    rows = []
    for rec in payload["data"]:
        period = f"{int(rec['revenue_year'])}-{int(rec['revenue_month']):02d}"
        rows.append({
            "period": period,
            "value": float(rec["revenue"]) / 1_000.0,   # TWD -> TWD thousands
            "unit": UNIT,
            "freq": "M",
            "source": "FinMind:TaiwanStockMonthRevenue",
            "retrieved": today(),
        })
    return rows


def from_twse_snapshot(tickers: dict[str, str]) -> list[dict]:
    """Newest month only. Keyed by ticker -> series_id."""
    data = _get(TWSE_OPENAPI).json()
    rows = []
    for rec in data:
        code = str(rec.get("公司代號", "")).strip()
        if code not in tickers:
            continue
        ym = str(rec.get("資料年月", "")).strip()      # e.g. '11505' (ROC) or '202605'
        if len(ym) == 5:                               # ROC year
            year, month = 1911 + int(ym[:3]), int(ym[3:])
        elif len(ym) == 6:
            year, month = int(ym[:4]), int(ym[4:])
        else:
            continue
        rows.append({
            "period": f"{year}-{month:02d}",
            "series_id": tickers[code],
            "value": float(str(rec["營業收入-當月營收"]).replace(",", "")),
            "unit": UNIT,
            "freq": "M",
            "source": "TWSE:t187ap05_L",
            "retrieved": today(),
        })
    return rows


def from_mops(year: int, month: int, tickers: dict[str, str]) -> list[dict]:
    """Authoritative dated archive. Returns every issuer for that month."""
    url = MOPS.format(roc=year - 1911, month=month)
    html = _get(url).content.decode("big5", errors="ignore")
    rows = []
    for table in pd.read_html(io.StringIO(html)):
        cols = [str(c) for c in table.columns]
        if not any("公司代號" in c for c in cols):
            continue
        code_col = next(c for c in table.columns if "公司代號" in str(c))
        rev_col = next(c for c in table.columns if "當月營收" in str(c))
        for _, r in table.iterrows():
            code = str(r[code_col]).strip()
            if code in tickers:
                rows.append({
                    "period": f"{year}-{month:02d}",
                    "series_id": tickers[code],
                    "value": pd.to_numeric(str(r[rev_col]).replace(",", ""), errors="coerce"),
                    "unit": UNIT,
                    "freq": "M",
                    "source": f"MOPS:t21sc03_{year}_{month}",
                    "retrieved": today(),
                })
    return rows


def run(start: str | None = None) -> pd.DataFrame:
    cfg = load_config()
    comps = cfg["taiwan_companies"]
    years = cfg["settings"]["history_years"]
    start = start or f"{pd.Timestamp.today().year - years}-01-01"
    by_ticker = {c["ticker"]: c["series_id"] for c in comps}

    rows: list[dict] = []
    for c in comps:
        try:
            got = from_finmind(c["ticker"], start)
            for g in got:
                g["series_id"] = c["series_id"]
            rows.extend(got)
            LOG.info("%-8s %3d months via FinMind", c["series_id"], len(got))
        except Exception as exc:
            LOG.error("%s FinMind failed (%s) - will rely on MOPS fallback", c["series_id"], exc)
        time.sleep(1.0)   # be polite to a free API

    # Overlay the authoritative snapshot for the newest month where available.
    try:
        snap = from_twse_snapshot(by_ticker)
        if snap:
            have = {(r["period"], r["series_id"]) for r in snap}
            rows = [r for r in rows if (r["period"], r["series_id"]) not in have] + snap
            LOG.info("overlaid %d rows from TWSE snapshot", len(snap))
    except Exception as exc:
        LOG.warning("TWSE snapshot unavailable (%s) - continuing", exc)

    df = tidy(rows)
    warns = validate(df, "twse", [c["series_id"] for c in comps])
    for w in warns:
        LOG.warning(w)
    save_raw(df, "taiwan_companies")
    (RAW / "twse_warnings.txt").write_text("\n".join(warns))
    return df


if __name__ == "__main__":
    run()
