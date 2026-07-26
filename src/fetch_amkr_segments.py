"""AMKR net sales split by product group (Advanced / Mainstream).

WHY THIS IS NOT IN fetch_amkr.py
SEC's companyconcept and companyfacts APIs return only UNDIMENSIONED facts -
the consolidated totals. Segment breakdowns live on dimensional contexts
(srt:ProductOrServiceAxis with an Advanced/Mainstream member) and simply are
not exposed by those endpoints. So the numbers have to come from the filings
themselves, which is what this module does: walk the submissions index, pull
each 10-Q/10-K's inline XBRL document, and read the facts whose context
carries the segment member.

This is more fragile than the companyconcept path. It is therefore wired in as
NON-CRITICAL: if it breaks, the aggregate forecast is untouched. A manual CSV
fallback (data/manual/amkr_segments.csv) exists for the same reason, seeded
with the quarters verified by hand.

DEFINITIONAL BREAK - read before extending history. 2019 filings define
Advanced products as flip chip and wafer-level processing. Current filings say
flip chip, MEMORY and wafer-level processing. Memory was reclassified into
Advanced, so the series is not continuous across that change and YoY growth
spanning it is partly an artifact of reclassification.
"""
from __future__ import annotations

import re
import time

import pandas as pd
import requests
from lxml import etree

from common import (MANUAL, load_config, log, read_manual_csv, save_raw, tidy,
                    today, validate)
from fetch_amkr import _headers

LOG = log("fetch_segments")

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"
FALLBACK = MANUAL / "amkr_segments.csv"

REVENUE_TAGS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
}


def _local(tag) -> str:
    """Local name of a tag, tolerating every shape lxml hands back.

    Parsing inline XBRL with the HTML parser (necessary - these documents are
    not well-formed XML) yields tags as plain strings with the namespace prefix
    intact, e.g. 'ix:nonfraction', while the XML parser yields Clark notation,
    '{uri}nonFraction'. Comments and PIs yield callables. QName chokes on the
    first form, so do the split by hand.
    """
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag


def _get(url: str, headers: dict, as_json: bool = False):
    for attempt in range(3):
        r = requests.get(url, headers={**headers, "Host": _host(url)}, timeout=60)
        if r.status_code == 200:
            return r.json() if as_json else r.content
        if r.status_code == 403:
            raise RuntimeError("SEC 403 - check SEC_USER_AGENT, or wait out an IP block")
        time.sleep(2 ** attempt)
    return None


def _host(url: str) -> str:
    return url.split("/")[2]


def parse_inline_xbrl(content: bytes, members: dict[str, str]) -> list[dict]:
    """Extract segment revenue facts from an inline-XBRL document.

    members maps xbrl member name fragment -> our series_id.
    Returns rows of {series_id, start, end, value}.
    """
    try:
        root = etree.fromstring(content, etree.HTMLParser(recover=True, huge_tree=True))
    except Exception as exc:
        LOG.warning("could not parse document: %s", exc)
        return []

    # --- contexts: id -> (member_series_id, start, end) -------------------
    ctx: dict[str, tuple[str, str, str]] = {}
    for el in root.iter():
        if _local(el.tag).lower() != "context":
            continue
        cid = el.get("id")
        if not cid:
            continue
        member_hit, start, end = None, None, None
        for sub in el.iter():
            name = _local(sub.tag).lower()
            if name == "explicitmember" and sub.text:
                for frag, sid in members.items():
                    if frag.lower() in sub.text.lower():
                        member_hit = sid
            elif name == "startdate":
                start = (sub.text or "").strip()
            elif name == "enddate":
                end = (sub.text or "").strip()
        if member_hit and start and end:
            ctx[cid] = (member_hit, start, end)

    if not ctx:
        return []

    # --- facts referencing those contexts ---------------------------------
    rows = []
    for el in root.iter():
        if _local(el.tag).lower() != "nonfraction":
            continue
        cref = el.get("contextref")
        if cref not in ctx:
            continue
        name = (el.get("name") or "").split(":")[-1]
        if name not in REVENUE_TAGS:
            continue
        raw = "".join(el.itertext()).strip().replace(",", "").replace("$", "")
        if not re.match(r"^-?\d+(\.\d+)?$", raw):
            continue
        val = float(raw) * (10 ** int(el.get("scale") or 0))
        if el.get("sign") == "-":
            val = -val
        sid, start, end = ctx[cref]
        rows.append({"series_id": sid, "start": start, "end": end, "value": val})
    return rows


def from_filings(cik: str, years: int) -> pd.DataFrame:
    cfg = load_config()
    members = {m["xbrl_member"]: m["series_id"] for m in cfg["segments"]["members"]}
    headers = _headers()

    subs = _get(SUBMISSIONS.format(cik=cik), headers, as_json=True)
    if not subs:
        raise RuntimeError("could not read the EDGAR submissions index")

    recent = subs.get("filings", {}).get("recent", {})
    frame = pd.DataFrame({k: recent[k] for k in
                          ("form", "accessionNumber", "primaryDocument", "reportDate")
                          if k in recent})
    frame = frame[frame.form.isin(["10-Q", "10-K"])]
    cutoff = (pd.Timestamp.today() - pd.DateOffset(years=years + 1)).strftime("%Y-%m-%d")
    frame = frame[frame.reportDate >= cutoff]
    LOG.info("%d 10-Q/10-K filings since %s", len(frame), cutoff)

    facts: list[dict] = []
    for _, f in frame.iterrows():
        url = ARCHIVE.format(cik_int=int(cik), acc_nodash=f.accessionNumber.replace("-", ""),
                             doc=f.primaryDocument)
        time.sleep(0.2)                       # SEC fair-access: stay under 10/sec
        content = _get(url, headers)
        if not content:
            LOG.warning("could not fetch %s", f.accessionNumber)
            continue
        got = parse_inline_xbrl(content, members)
        facts.extend(got)
        LOG.info("  %s %s -> %d segment facts", f.form, f.reportDate, len(got))

    if not facts:
        raise RuntimeError("no dimensional segment facts found in any filing")
    return _to_quarters(pd.DataFrame(facts))


def _to_quarters(df: pd.DataFrame) -> pd.DataFrame:
    """Keep ~quarterly durations, dedupe, and derive Q4 from the annual figure."""
    df["start"] = pd.to_datetime(df["start"])
    df["end"] = pd.to_datetime(df["end"])
    df["days"] = (df["end"] - df["start"]).dt.days

    q = df[df.days.between(80, 100)].copy()
    q["quarter"] = q["end"].dt.year.astype(str) + "Q" + q["end"].dt.quarter.astype(str)
    q = q.drop_duplicates(["series_id", "quarter"], keep="first")

    a = df[df.days.between(350, 380)].copy()
    a["year"] = a["end"].dt.year
    a = a.drop_duplicates(["series_id", "year"], keep="first")

    have = {(r.series_id, r.quarter): r.value for r in q.itertuples()}
    for r in a.itertuples():
        y, sid = int(r.year), r.series_id
        first3 = [have.get((sid, f"{y}Q{i}")) for i in (1, 2, 3)]
        if (sid, f"{y}Q4") not in have and all(v is not None for v in first3):
            have[(sid, f"{y}Q4")] = float(r.value) - sum(first3)
            LOG.info("derived %s %sQ4 from the annual figure", sid, y)

    return tidy([{
        "period": qtr, "series_id": sid, "value": v / 1_000_000.0,
        "unit": "USD_MILLIONS", "freq": "Q",
        "source": "SEC:inline XBRL (dimensional)", "retrieved": today(),
    } for (sid, qtr), v in sorted(have.items())])


def from_fallback() -> pd.DataFrame:
    if not FALLBACK.exists():
        return tidy([])
    raw = read_manual_csv(FALLBACK, ["advanced_usdm", "mainstream_usdm"],
                          period_col="quarter")
    raw = raw[~raw.get("notes", pd.Series(dtype=str))
              .astype(str).str.upper().str.startswith("EXAMPLE", na=False)]
    rows = []
    for _, r in raw.iterrows():
        for col, sid in [("advanced_usdm", "AMKR_ADVANCED"),
                         ("mainstream_usdm", "AMKR_MAINSTREAM")]:
            if pd.notna(r.get(col)):
                rows.append({
                    "period": str(r["quarter"]).strip(), "series_id": sid,
                    "value": float(r[col]), "unit": "USD_MILLIONS", "freq": "Q",
                    "source": "manual (10-Q product group table)", "retrieved": today(),
                })
    LOG.info("loaded %d segment rows from the manual fallback", len(rows))
    return tidy(rows)


def run() -> pd.DataFrame:
    cfg = load_config()
    if not cfg.get("segments", {}).get("enabled"):
        raise RuntimeError("segments disabled in config")
    try:
        df = from_filings(cfg["target"]["cik"], cfg["settings"]["history_years"])
        LOG.info("parsed %d segment rows from filings", len(df))
    except Exception as exc:
        LOG.error("filing parse failed (%s) - falling back to manual CSV", exc)
        df = from_fallback()

    if df.empty:
        raise RuntimeError(
            "no segment data from filings or the manual fallback. Fill "
            "data/manual/amkr_segments.csv from the 10-Q 'Product Groups' table."
        )
    for w in validate(df, "segments", [m["series_id"] for m in cfg["segments"]["members"]]):
        LOG.warning(w)
    save_raw(df, "amkr_segments")
    return df


if __name__ == "__main__":
    run()
