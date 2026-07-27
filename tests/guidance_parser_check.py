"""Fixture test for the guidance prose parser.

Guidance is extracted from press-release prose, so the failure modes are
linguistic rather than structural. These fixtures cover the phrasings Amkor has
actually used, plus the traps that would produce a confidently wrong number:

  * unit on the second figure only ("$1.75 to $1.85 billion")
  * millions rather than billions
  * an EPS range in the same document, which must never be read as revenue
  * a gross margin range, likewise
  * historical results prose with no guidance at all -> must return None

A parser that returns nothing is recoverable. A parser that returns an EPS
range as revenue guidance is not, because nothing downstream will notice.

Run:  python tests/guidance_parser_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CASES = [
    ("billions, explicit outlook",
     "Business Outlook. For the second quarter of 2026, Amkor expects net sales "
     "of $1.75 billion to $1.85 billion.",
     (1750.0, 1850.0)),

    ("unit on second figure only",
     "The company expects net sales of $1.60 to $1.70 billion for the first "
     "quarter of 2026.",
     (1600.0, 1700.0)),

    ("stated in millions with commas",
     "Amkor anticipates net sales in the range of $1,750 million to $1,850 "
     "million for the coming quarter.",
     (1750.0, 1850.0)),

    ("'between X and Y' phrasing",
     "For the third quarter, net sales are expected to be between $1.90 billion "
     "and $2.00 billion.",
     (1900.0, 2000.0)),

    ("en-dash range",
     "Outlook: net sales of $1.45 billion – $1.55 billion.",
     (1450.0, 1550.0)),

    # --- the case that failed on real filings ------------------------------
    # Amkor's Business Outlook is a TABLE. Flattened to text it has no sentence
    # punctuation, so net sales, margin and EPS ranges all sit in one run.
    ("flattened outlook table",
     "Business Outlook Net sales $1,750 million to $1,850 million "
     "Gross margin 14.5% to 16.5% Net income per diluted share $0.30 to $0.40 "
     "Capital expenditures $850 million",
     (1750.0, 1850.0)),

    ("table with EPS listed FIRST",
     "Outlook Net income per diluted share $0.30 to $0.40 "
     "Net sales $1,900 million to $2,000 million Gross margin 15% to 17%",
     (1900.0, 2000.0)),

    ("table with no net sales line at all",
     "Outlook Gross margin 14.5% to 16.5% Net income per diluted share "
     "$0.30 to $0.40",
     None),

    # --- traps -------------------------------------------------------------
    ("EPS range must be ignored",
     "The company expects earnings per share of $0.30 to $0.40 for the quarter.",
     None),

    ("gross margin range must be ignored",
     "Amkor expects gross margin of 14.5% to 16.5% in the second quarter.",
     None),

    ("historical results only, no guidance",
     "Second quarter net sales were $1.46 billion, compared with $1.44 billion "
     "in the prior year period.",
     None),
]

QUARTER_CASES = [
    ("Amkor announced financial results for the first quarter ended March 31, 2026.",
     "2026Q1"),
    ("results for the fourth quarter and full year ended December 31, 2025", "2025Q4"),
    ("financial results for the second quarter ended June 30, 2024", "2024Q2"),
]


def main() -> int:
    from fetch_amkr_guidance import parse_guidance_text, _reported_quarter

    checks = []
    for name, text, expected in CASES:
        got = parse_guidance_text(text)
        if expected is None:
            ok = got is None
            detail = "" if ok else f"wrongly parsed {got['guide_low_usdm']}-{got['guide_high_usdm']}"
        else:
            ok = (got is not None
                  and abs(got["guide_low_usdm"] - expected[0]) < 0.01
                  and abs(got["guide_high_usdm"] - expected[1]) < 0.01)
            detail = (f"${got['guide_low_usdm']:.0f}-{got['guide_high_usdm']:.0f}m"
                      if got else "returned None")
        checks.append((name, ok, detail))

    for text, expected in QUARTER_CASES:
        got = _reported_quarter(text)
        checks.append((f"quarter from '{text[:34]}...'", got == expected,
                       f"got {got}"))

    # Guidance applies to the quarter AFTER the one being reported.
    from common import next_quarter
    checks.append(("guidance maps to the following quarter",
                   next_quarter("2026Q1") == "2026Q2", ""))
    checks.append(("year rolls over at Q4", next_quarter("2025Q4") == "2026Q1", ""))

    print("=" * 74)
    failed = 0
    for name, ok, detail in checks:
        failed += (not ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<44} {detail}")
    print("=" * 74)
    print(f"{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
