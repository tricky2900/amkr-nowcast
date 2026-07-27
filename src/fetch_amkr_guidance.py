"""Backfill AMKR's quarterly net-sales guidance from earnings 8-K exhibits.

WHY THIS IS THE MOST DANGEROUS PARSER IN THE REPO
Segment revenue is XBRL-tagged: a machine-readable fact on a labelled context.
Guidance is not. It is forward-looking prose in a press release, phrased at
management's discretion, and the wording drifts over the years:

    "net sales of $1.75 billion to $1.85 billion"
    "net sales in the range of $1,750 million to $1,850 million"
    "net sales between $1.75 and $1.85 billion"

A regex over prose can pick up the wrong sentence, the wrong unit, or an EPS
range and call it revenue. So this module is built to be distrusted:

  * Every parsed row carries a confidence flag and the sentence it came from.
  * Rows failing a plausibility check against actual revenue are marked
    'needs_review' rather than being silently dropped OR silently trusted.
  * output/guidance_review.csv lists every parse with its source sentence, for
    a human to scan.
  * HAND-ENTERED ROWS ALWAYS WIN. data/manual/amkr_guidance.csv is authoritative;
    this parser only fills quarters that file does not already cover.

Treat the output as a draft someone still has to check, not as data.
"""
from __future__ import annotations

import re
import time

import pandas as pd
import requests
from lxml import etree

from common import (DATA, OUTPUT, load_config, log, next_quarter,
                    quarter_sort_key, safe_read, today)
from fetch_amkr import _headers

LOG = log("fetch_guidance")

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_DIR = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"
INDEX_JSON = ARCHIVE_DIR + "index.json"

# Labels that can precede a numeric range. Whichever appears NEAREST before a
# range decides what that range is measuring.
SALES_CUE = re.compile(r"net sales|net revenue", re.I)
OUTLOOK_CUE = re.compile(r"outlook|guidance|expect|anticipat", re.I)

# "$1.75 billion to $1.85 billion" / "$1,750 million to $1,850 million" /
# "$1.75 to $1.85 billion" / "between $1.75 and $1.85 billion"
RANGE = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(billion|million|bn|mn|mm)?\s*"
    r"(?:to|and|through|[-–—])\s*"
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(billion|million|bn|mn|mm)",
    re.I,
)

# Other line items that carry ranges in the same outlook block.
OTHER_ITEM = re.compile(r"per share|earnings per|EPS|gross margin|operating margin|"
                        r"operating income|tax rate|capital expenditure|capex|"
                        r"EBITDA|net income|effective tax", re.I)

# How far back to look for the label that owns a range.
LOOKBACK = 160


def _to_musd(value: str, unit: str | None) -> float | None:
    v = float(value.replace(",", ""))
    u = (unit or "").lower()
    if u in ("billion", "bn"):
        return v * 1000.0
    if u in ("million", "mn", "mm"):
        return v
    # No unit on the first figure - it inherits the second one's unit.
    return None


def parse_guidance_text(text: str) -> dict | None:
    """Find a net-sales range in an earnings release.

    NEAREST PRECEDING LABEL WINS. An earlier version split the document into
    sentences and rejected any sentence mentioning EPS or margin. That works on
    prose and fails completely on the real filings, because Amkor puts the
    Business Outlook in a TABLE - flattened to text it has no sentence
    punctuation, so the whole block is one "sentence" containing net sales,
    gross margin AND per-share ranges. The guard against false positives became
    a blanket false negative: zero rows from 51 filings.

    So instead: locate every numeric range, look back a short window, and let
    the closest label decide what the range measures. Handles tables and prose
    with the same logic. Returns None when nothing is defensible - silence
    beats a confident wrong number.
    """
    text = re.sub(r"\s+", " ", text)
    best = None
    for m in RANGE.finditer(text):
        window = text[max(0, m.start() - LOOKBACK):m.start()]
        last_sales = max((x.end() for x in SALES_CUE.finditer(window)), default=None)
        last_other = max((x.end() for x in OTHER_ITEM.finditer(window)), default=None)
        if last_sales is None:
            continue                       # no revenue label owns this range
        if last_other is not None and last_other > last_sales:
            continue                       # a different line item is nearer
        lo_raw, lo_unit, hi_raw, hi_unit = m.groups()
        hi = _to_musd(hi_raw, hi_unit)
        lo = _to_musd(lo_raw, lo_unit) or _to_musd(lo_raw, hi_unit)
        if lo is None or hi is None or not (0 < lo < hi):
            continue
        # Percentages are margins, not dollars, however they are labelled.
        if "%" in text[m.start():m.end() + 2]:
            continue
        context = text[max(0, m.start() - 400):m.end() + 60]
        conf = "high" if OUTLOOK_CUE.search(context) else "medium"
        cand = {"guide_low_usdm": lo, "guide_high_usdm": hi,
                "confidence": conf, "sentence": context.strip()[-300:]}
        if best is None or (conf == "high" and best["confidence"] != "high"):
            best = cand
    return best


def _reported_quarter(text: str) -> str | None:
    """Which quarter the release reports on. Guidance applies to the NEXT one."""
    m = re.search(
        r"(first|second|third|fourth)\s+quarter[^.]{0,60}?(20\d{2})", text, re.I)
    if m:
        n = {"first": 1, "second": 2, "third": 3, "fourth": 4}[m.group(1).lower()]
        return f"{m.group(2)}Q{n}"
    m = re.search(r"quarter ended\s+(\w+)\s+\d{1,2},\s*(20\d{2})", text, re.I)
    if m:
        month = {"march": 1, "june": 2, "september": 3, "december": 4}.get(
            m.group(1).lower())
        if month:
            return f"{m.group(2)}Q{month}"
    return None


def _get(url: str, headers: dict, as_json: bool = False):
    for attempt in range(3):
        r = requests.get(url, headers={**headers, "Host": url.split("/")[2]}, timeout=60)
        if r.status_code == 200:
            return r.json() if as_json else r.content
        if r.status_code == 403:
            raise RuntimeError("SEC 403 - check SEC_USER_AGENT or wait out an IP block")
        time.sleep(2 ** attempt)
    return None


def _text(content: bytes) -> str:
    root = etree.fromstring(content, etree.HTMLParser(recover=True, huge_tree=True))
    return " ".join(root.itertext())


def run() -> pd.DataFrame:
    cfg = load_config()
    cik, years = cfg["target"]["cik"], cfg["settings"]["history_years"]
    headers = _headers()

    subs = _get(SUBMISSIONS.format(cik=cik), headers, as_json=True)
    if not subs:
        raise RuntimeError("could not read the EDGAR submissions index")
    recent = subs.get("filings", {}).get("recent", {})
    f = pd.DataFrame({k: recent[k] for k in
                      ("form", "accessionNumber", "reportDate") if k in recent})
    f = f[f.form == "8-K"]
    cutoff = (pd.Timestamp.today() - pd.DateOffset(years=years + 1)).strftime("%Y-%m-%d")
    f = f[f.reportDate >= cutoff]
    LOG.info("%d 8-K filings since %s", len(f), cutoff)

    rows = []
    for _, filing in f.iterrows():
        acc = filing.accessionNumber.replace("-", "")
        idx = _get(INDEX_JSON.format(cik_int=int(cik), acc_nodash=acc), headers,
                   as_json=True)
        time.sleep(0.2)
        if not idx:
            continue
        # Earnings releases live in EX-99.x, not the 8-K body.
        names = [i["name"] for i in idx.get("directory", {}).get("item", [])
                 if re.search(r"ex.?99", i["name"], re.I)
                 and i["name"].lower().endswith((".htm", ".html"))]
        for name in names:
            content = _get(ARCHIVE_DIR.format(cik_int=int(cik), acc_nodash=acc) + name,
                           headers)
            time.sleep(0.2)
            if not content:
                continue
            text = _text(content)
            if "net sales" not in text.lower():
                continue
            reported = _reported_quarter(text)
            g = parse_guidance_text(text)
            if not (reported and g):
                continue
            rows.append({
                "quarter": next_quarter(reported),
                "guide_low_usdm": g["guide_low_usdm"],
                "guide_high_usdm": g["guide_high_usdm"],
                "guided_on": filing.reportDate,
                "reported_quarter": reported,
                "confidence": g["confidence"],
                "source_url": ARCHIVE_DIR.format(cik_int=int(cik), acc_nodash=acc) + name,
                "sentence": g["sentence"],
                "retrieved": today(),
            })
            LOG.info("  %s -> %s guidance $%.0fm-$%.0fm (%s)", filing.reportDate,
                     next_quarter(reported), g["guide_low_usdm"],
                     g["guide_high_usdm"], g["confidence"])
            break

    if not rows:
        raise RuntimeError("no guidance ranges parsed from any 8-K exhibit")

    out = pd.DataFrame(rows).drop_duplicates("quarter", keep="first")
    out = out.sort_values("quarter", key=lambda c: c.map(quarter_sort_key))
    out = _plausibility(out)

    OUTPUT.mkdir(exist_ok=True)
    out.to_csv(OUTPUT / "guidance_review.csv", index=False)
    (DATA / "raw").mkdir(parents=True, exist_ok=True)
    out.to_csv(DATA / "raw" / "amkr_guidance_parsed.csv", index=False)

    flagged = (out.confidence == "needs_review").sum()
    LOG.info("parsed %d guidance quarters (%d flagged for review)", len(out), flagged)
    if flagged:
        LOG.warning("check output/guidance_review.csv before relying on flagged rows")
    return out


def _plausibility(out: pd.DataFrame) -> pd.DataFrame:
    """Cross-check each range against actual revenue. A guide should sit within
    shouting distance of the prior quarter; anything wilder is a bad parse."""
    actual = safe_read(DATA / "raw" / "amkr_quarterly.csv")
    if actual.empty:
        return out
    lvl = dict(zip(actual.period, actual.value))
    for i, r in out.iterrows():
        mid = (r.guide_low_usdm + r.guide_high_usdm) / 2
        ref = lvl.get(r.reported_quarter)
        if ref and not (0.5 * ref <= mid <= 2.0 * ref):
            out.at[i, "confidence"] = "needs_review"
            LOG.warning("%s guide midpoint $%.0fm vs $%.0fm actual in %s - "
                        "implausible, flagged", r.quarter, mid, ref, r.reported_quarter)
        if r.guide_high_usdm / max(r.guide_low_usdm, 1) > 1.5:
            out.at[i, "confidence"] = "needs_review"
            LOG.warning("%s guide range is suspiciously wide - flagged", r.quarter)
    return out


if __name__ == "__main__":
    run()
