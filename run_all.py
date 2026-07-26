#!/usr/bin/env python3
"""Run the whole pipeline: fetch -> build -> analyse -> scenarios -> outputs.

Failures are classified, because treating them all alike is how alerting stops
working. If the MOF portal breaks every month and that turns the job red every
month, you will start ignoring the red - and then miss the run where something
that actually matters broke.

  FATAL     no forecast is possible. Exit 1, open an issue.
            (Taiwan peer revenue, AMKR actuals, the build, the model)
  DEGRADED  the forecast still stands, with fewer inputs. Exit 0, but say so
            loudly in the status file and the log.
            (Taiwan exports, SIA billings - supporting series, not core signal)

Usage
    python run_all.py                 full refresh
    python run_all.py --no-fetch      rebuild + re-analyse from data on disk
    python run_all.py --only-fetch    pull data, skip analysis
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from common import OUTPUT, log  # noqa: E402

LOG = log("run_all")

fatal: list[str] = []
degraded: list[str] = []


def _step(name: str, fn, critical: bool) -> bool:
    try:
        fn()
        LOG.info("OK       %s", name)
        return True
    except Exception as exc:
        (fatal if critical else degraded).append(f"{name}: {exc}")
        LOG.error("%s %s -> %s", "FATAL   " if critical else "DEGRADED", name, exc)
        LOG.debug(traceback.format_exc())
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--only-fetch", action="store_true")
    args = ap.parse_args()

    if not args.no_fetch:
        import fetch_amkr, fetch_amkr_guidance, fetch_amkr_segments
        import fetch_mof, fetch_twse, manual_sources
        _step("taiwan companies", fetch_twse.run, critical=True)
        _step("amkr quarterly", fetch_amkr.run, critical=True)
        # Segment data needs dimensional XBRL parsed out of the filings, which
        # is more fragile than the companyconcept API. Non-critical by design:
        # if it breaks, the aggregate forecast is unaffected.
        _step("amkr segments", fetch_amkr_segments.run, critical=False)
        # Guidance is parsed from press-release PROSE, not tagged XBRL - the
        # least reliable source here. Non-critical, and hand-entered rows in
        # data/manual/amkr_guidance.csv always take precedence over it.
        _step("amkr guidance", fetch_amkr_guidance.run, critical=False)
        _step("taiwan exports", fetch_mof.run, critical=False)
        _step("sia billings", manual_sources.load_sia, critical=False)

    if args.only_fetch:
        return _finish()

    import analyze, build_master, make_charts, make_excel, scenarios
    if not _step("build master", build_master.run, critical=True):
        return _finish()
    if not _step("analyze", analyze.run, critical=True):
        # Without a model there is nothing to score, but the database itself is
        # still worth writing out - it is the part your Excel model links to.
        _step("excel", make_excel.run, critical=False)
        return _finish()

    _step("scenarios", scenarios.run, critical=True)
    # Segment model is an additional read, never a replacement for the
    # aggregate forecast. Failure here must not turn the run red.
    import segment_model
    _step("segment model", segment_model.run, critical=False)
    import guidance_analysis
    _step("guidance analysis", guidance_analysis.run, critical=False)
    _step("excel", make_excel.run, critical=True)
    _step("charts", make_charts.run, critical=False)
    return _finish()


def _coverage() -> list[str]:
    """Per-series row counts and latest period. A source can fail without
    throwing - by returning nothing - so the status file states what actually
    landed rather than only what errored."""
    import pandas as pd
    from common import DATA

    out = ["", "COVERAGE - what is actually in the database:"]
    for label, path, key in [("monthly", DATA / "master_monthly.csv", "period"),
                             ("quarterly", DATA / "master_quarterly.csv", "quarter")]:
        if not path.exists() or path.stat().st_size == 0:
            out.append(f"  {label}: MISSING")
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            out.append(f"  {label}: unreadable")
            continue
        if df.empty:
            out.append(f"  {label}: empty")
            continue
        out.append(f"  {label}:")
        for sid, grp in df.groupby("series_id"):
            out.append(f"    {sid:<16} {len(grp):>4} rows   latest {grp[key].max()}")
    return out


def _finish() -> int:
    OUTPUT.mkdir(exist_ok=True)
    lines: list[str] = []
    if fatal:
        lines += ["FATAL - no forecast produced this run:"]
        lines += [f"  - {f}" for f in fatal]
    if degraded:
        lines += ["", "DEGRADED - forecast still valid, but these inputs are missing:"]
        lines += [f"  - {d}" for d in degraded]
    if not fatal and not degraded:
        lines = ["All steps completed successfully."]
    elif not fatal:
        lines += ["", "Core pipeline succeeded. Exiting 0."]

    lines += _coverage()
    text = "\n".join(lines) + "\n"
    (OUTPUT / "run_status.txt").write_text(text)
    print("\n" + text)

    if fatal:
        LOG.error("%d fatal, %d degraded", len(fatal), len(degraded))
        return 1
    if degraded:
        LOG.warning("completed with %d degraded input(s)", len(degraded))
    else:
        LOG.info("pipeline complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
