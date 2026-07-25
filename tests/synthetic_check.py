"""End-to-end pipeline check on synthetic data with a KNOWN relationship.

This does not validate the fetchers (they need live internet). It validates
everything after them: quarterly aggregation, YoY, lead/lag detection, the
matched-horizon refit, guidance anchoring, Excel and charts.

The synthetic world is built so we know the right answer in advance:
peers lead AMKR by construction, so a correct pipeline must recover a strong
positive correlation and a sensible beta. If this script prints FAIL, the
analysis layer is broken regardless of what the real data says.

Run:  python tests/synthetic_check.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import common  # noqa: E402

RNG = np.random.default_rng(20260725)

# Mirror the live situation as of late July 2026: Taiwan has filed through
# June, AMKR has reported through 2026Q1 and reports 2026Q2 shortly.
MONTHS = pd.period_range("2019-01", "2026-06", freq="M")
AMKR_LAST_Q = "2026Q1"


def _cycle(n: int) -> np.ndarray:
    t = np.arange(n)
    return (0.22 * np.sin(2 * np.pi * t / 42)          # ~3.5y semi cycle
            + 0.10 * np.sin(2 * np.pi * t / 13 + 1.1))  # shorter wobble


def build_synthetic() -> None:
    raw = ROOT / "data" / "raw"
    if raw.exists():
        shutil.rmtree(raw)
    raw.mkdir(parents=True, exist_ok=True)

    n = len(MONTHS)
    cyc = _cycle(n)
    seas = 0.05 * np.sin(2 * np.pi * (np.arange(n) % 12) / 12)
    periods = [f"{p.year}-{p.month:02d}" for p in MONTHS]

    rows = []
    scale = {"TSMC": 180e6, "ASE": 48e6, "KYEC": 2.6e6, "CHIPMOS": 1.9e6, "PTI": 6.2e6}
    for sid, base in scale.items():
        # TSMC leads by a month; OSATs are coincident.
        shift = 1 if sid == "TSMC" else 0
        drift = np.linspace(0, 0.35, n)
        c = np.roll(cyc, -shift)
        noise = RNG.normal(0, 0.030, n)
        vals = base * (1 + drift) * (1 + c + seas + noise)
        for p, v in zip(periods, vals):
            rows.append({"period": p, "series_id": sid, "value": float(v),
                         "unit": "TWD_THOUSANDS", "freq": "M",
                         "source": "SYNTHETIC", "retrieved": common.today()})
    common.tidy(rows).to_csv(raw / "taiwan_companies.csv", index=False)

    # Taiwan exports, correlated but noisier.
    erows = []
    for sid, base in [("TWEX_TOTAL", 34000), ("TWEX_ELEC_PARTS", 12500),
                      ("TWEX_ICT_AV", 6200), ("TWEX_ELEC_ICT", 18700)]:
        vals = base * (1 + np.linspace(0, .30, n)) * (1 + 0.8 * cyc + seas
                                                      + RNG.normal(0, .045, n))
        for p, v in zip(periods, vals):
            erows.append({"period": p, "series_id": sid, "value": float(v),
                          "unit": "USD_MILLIONS", "freq": "M",
                          "source": "SYNTHETIC", "retrieved": common.today()})
    common.tidy(erows).to_csv(raw / "taiwan_exports.csv", index=False)

    # AMKR quarterly: driven by the same cycle, lagging the peers slightly.
    qrows = []
    qmap: dict[str, list[float]] = {}
    for i, p in enumerate(periods):
        qmap.setdefault(common.month_to_quarter(p), []).append(cyc[i])
    quarters = sorted(qmap, key=common.quarter_sort_key)
    base_q, drift_q = 1150.0, np.linspace(0, .40, len(quarters))
    for i, q in enumerate(quarters):
        if common.quarter_sort_key(q) > common.quarter_sort_key(AMKR_LAST_Q):
            continue          # AMKR has not reported these yet
        c = float(np.mean(qmap[q]))
        val = base_q * (1 + drift_q[i]) * (1 + 0.95 * c + RNG.normal(0, .022))
        qrows.append({"period": q, "series_id": "AMKR", "value": float(val),
                      "unit": "USD_MILLIONS", "freq": "Q",
                      "source": "SYNTHETIC", "retrieved": common.today()})
    common.tidy(qrows).to_csv(raw / "amkr_quarterly.csv", index=False)
    print(f"synthetic: {len(periods)} months, AMKR through {AMKR_LAST_Q}")


def main() -> int:
    build_synthetic()
    import analyze, build_master, make_charts, make_excel, scenarios

    checks: list[tuple[str, bool, str]] = []

    built = build_master.run()
    q = built["quarterly"]
    checks.append(("master built", not q.empty, f"{q.quarter.nunique()} quarters"))

    qtd = pd.read_csv(ROOT / "data" / "qtd.csv")
    target = qtd.quarter.iloc[0] if not qtd.empty else None
    checks.append(("nowcast target is 2026Q2", target == "2026Q2", f"got {target}"))
    checks.append(("peer data complete for target",
                   (not qtd.empty) and qtd.months_reported.max() == 3,
                   f"max months={qtd.months_reported.max() if not qtd.empty else 0}"))

    res = analyze.run()
    ll, model = res["lead_lag"], res["model"]
    top_corr = ll["corr"].max()
    checks.append(("recovers strong peer correlation", top_corr > 0.75, f"max r={top_corr:.2f}"))
    checks.append(("beta is positive and sane", 0.3 < model["beta"] < 2.5,
                   f"beta={model['beta']:.2f}"))
    checks.append(("backtest ran", model["backtest_rmse"] == model["backtest_rmse"],
                   f"RMSE={model['backtest_rmse']*100:.1f}pp"))
    tsmc = ll[ll.series_id == "TSMC"]
    if not tsmc.empty:
        checks.append(("TSMC detected as leading", int(tsmc.lead_months.iloc[0]) >= 1,
                       f"lead={int(tsmc.lead_months.iloc[0])}m"))

    sc = scenarios.run()
    ok = (not sc.empty) and (sc.bear_usdm < sc.base_usdm).all() and (sc.base_usdm < sc.bull_usdm).all()
    checks.append(("bear < base < bull", ok, "" if ok else "ordering violated"))
    if not sc.empty:
        t = sc.iloc[0]
        checks.append(("guidance anchoring applied", "guidance" in str(t.anchor), str(t.anchor)))
        checks.append(("bear <= guide low", t.bear_usdm <= t.guide_low_usdm + 1e-6,
                       f"bear={t.bear_usdm:.0f} guide_low={t.guide_low_usdm:.0f}"))
        checks.append(("bull >= guide high", t.bull_usdm >= t.guide_high_usdm - 1e-6,
                       f"bull={t.bull_usdm:.0f} guide_high={t.guide_high_usdm:.0f}"))

    xl = make_excel.run()
    checks.append(("excel written", Path(xl).exists(), Path(xl).name))
    make_charts.run()
    pngs = list((ROOT / "output").glob("*.png"))
    checks.append(("charts written", len(pngs) >= 3, f"{len(pngs)} png"))

    print("\n" + "=" * 72)
    failed = 0
    for name, ok, detail in checks:
        flag = "PASS" if ok else "FAIL"
        failed += (not ok)
        print(f"  [{flag}] {name:<34} {detail}")
    print("=" * 72)
    print(f"{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
