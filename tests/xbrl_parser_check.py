"""Fixture test for the inline-XBRL segment parser.

The parser cannot be exercised against SEC from a sandbox, so it is tested
against a hand-built fragment that reproduces the structures that actually
matter in an Amkor 10-Q:

  * a dimensional context carrying an Advanced/Mainstream member
  * an UNDIMENSIONED context for the consolidated total, which must be ignored
    (picking it up would silently double-count revenue as a segment)
  * a prior-year comparative period in the same document
  * scale="3" (thousands), which must be applied
  * a non-revenue tag on a dimensional context, which must be ignored

Run:  python tests/xbrl_parser_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURE = b"""
<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
<body>
<div style="display:none">
  <ix:header><ix:resources>

    <xbrli:context id="C_ADV_2026Q1">
      <xbrli:entity><xbrli:segment>
        <xbrldi:explicitMember dimension="srt:ProductOrServiceAxis">amkr:AdvancedProductsMember</xbrldi:explicitMember>
      </xbrli:segment></xbrli:entity>
      <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
    </xbrli:context>

    <xbrli:context id="C_MAIN_2026Q1">
      <xbrli:entity><xbrli:segment>
        <xbrldi:explicitMember dimension="srt:ProductOrServiceAxis">amkr:MainstreamProductsMember</xbrldi:explicitMember>
      </xbrli:segment></xbrli:entity>
      <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
    </xbrli:context>

    <xbrli:context id="C_ADV_2025Q1">
      <xbrli:entity><xbrli:segment>
        <xbrldi:explicitMember dimension="srt:ProductOrServiceAxis">amkr:AdvancedProductsMember</xbrldi:explicitMember>
      </xbrli:segment></xbrli:entity>
      <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period>
    </xbrli:context>

    <!-- consolidated total: no segment dimension, must NOT be picked up -->
    <xbrli:context id="C_TOTAL_2026Q1">
      <xbrli:entity/>
      <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
    </xbrli:context>

  </ix:resources></ix:header>
</div>

<ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
  contextRef="C_ADV_2026Q1" scale="3" unitRef="usd">1,372,001</ix:nonFraction>
<ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
  contextRef="C_MAIN_2026Q1" scale="3" unitRef="usd">312,700</ix:nonFraction>
<ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
  contextRef="C_ADV_2025Q1" scale="3" unitRef="usd">1,063,617</ix:nonFraction>
<ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
  contextRef="C_TOTAL_2026Q1" scale="3" unitRef="usd">1,684,701</ix:nonFraction>
<ix:nonFraction name="us-gaap:GrossProfit"
  contextRef="C_ADV_2026Q1" scale="3" unitRef="usd">239,000</ix:nonFraction>
</body></html>
"""

MEMBERS = {"AdvancedProductsMember": "AMKR_ADVANCED",
           "MainstreamProductsMember": "AMKR_MAINSTREAM"}


def main() -> int:
    from fetch_amkr_segments import parse_inline_xbrl, _to_quarters
    import pandas as pd

    rows = parse_inline_xbrl(FIXTURE, MEMBERS)
    checks = []

    checks.append(("parsed 3 dimensional facts", len(rows) == 3, f"got {len(rows)}"))

    by = {(r["series_id"], r["end"]): r["value"] for r in rows}
    adv = by.get(("AMKR_ADVANCED", "2026-03-31"))
    checks.append(("scale=3 applied to Advanced", adv == 1_372_001_000,
                   f"got {adv:,.0f}" if adv else "missing"))
    main_v = by.get(("AMKR_MAINSTREAM", "2026-03-31"))
    checks.append(("Mainstream parsed", main_v == 312_700_000,
                   f"got {main_v:,.0f}" if main_v else "missing"))
    checks.append(("prior-year comparative captured",
                   ("AMKR_ADVANCED", "2025-03-31") in by, ""))

    vals = [r["value"] for r in rows]
    checks.append(("undimensioned total ignored", 1_684_701_000 not in vals,
                   "consolidated total leaked in" if 1_684_701_000 in vals else ""))
    checks.append(("non-revenue tag ignored", 239_000_000 not in vals,
                   "GrossProfit leaked in" if 239_000_000 in vals else ""))

    q = _to_quarters(pd.DataFrame(rows))
    got = dict(zip(zip(q.series_id, q.period), q.value))
    checks.append(("mapped to 2026Q1 in USD millions",
                   abs(got.get(("AMKR_ADVANCED", "2026Q1"), 0) - 1372.001) < 0.01,
                   f"{got.get(('AMKR_ADVANCED', '2026Q1'), 0):,.3f}"))
    checks.append(("Advanced share ~81.4%",
                   abs(1372.001 / (1372.001 + 312.700) - 0.8144) < 0.001, ""))

    print("=" * 68)
    failed = 0
    for name, ok, detail in checks:
        failed += (not ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<34} {detail}")
    print("=" * 68)
    print(f"{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
