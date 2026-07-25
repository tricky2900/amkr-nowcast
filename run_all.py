#!/usr/bin/env python3
"""Run the whole pipeline: fetch -> build -> analyse -> scenarios -> outputs.

Fetch failures are tolerated individually (one dead source should not stop the
refresh) but recorded, and the process exits non-zero if anything failed so the
GitHub Actions run goes red and opens an alert issue rather than silently
committing a stale or partial database.

Usage
    python run_all.py                 full refresh
    python run_all.py --no-fetch      rebuild + re-analyse from data already on disk
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--only-fetch", action="store_true")
    args = ap.parse_args()

    failures: list[str] = []

    if not args.no_fetch:
        import fetch_twse, fetch_mof, fetch_amkr, manual_sources
        for name, fn in [
            ("taiwan companies", fetch_twse.run),
            ("taiwan exports", fetch_mof.run),
            ("amkr quarterly", fetch_amkr.run),
            ("sia billings", manual_sources.load_sia),
        ]:
            try:
                fn()
                LOG.info("OK   %s", name)
            except Exception as exc:
                failures.append(f"{name}: {exc}")
                LOG.error("FAIL %s -> %s", name, exc)
                LOG.debug(traceback.format_exc())

    if args.only_fetch:
        return _finish(failures)

    import build_master, analyze, scenarios, make_excel, make_charts
    try:
        build_master.run()
    except Exception as exc:
        failures.append(f"build_master: {exc}")
        LOG.error("FAIL build_master -> %s", exc)
        return _finish(failures)

    for name, fn in [("analyze", analyze.run), ("scenarios", scenarios.run),
                     ("excel", make_excel.run), ("charts", make_charts.run)]:
        try:
            fn()
            LOG.info("OK   %s", name)
        except Exception as exc:
            failures.append(f"{name}: {exc}")
            LOG.error("FAIL %s -> %s", name, exc)
            LOG.debug(traceback.format_exc())

    return _finish(failures)


def _finish(failures: list[str]) -> int:
    OUTPUT.mkdir(exist_ok=True)
    report = OUTPUT / "run_status.txt"
    if failures:
        report.write_text("FAILURES\n" + "\n".join(f"- {f}" for f in failures))
        LOG.error("%d step(s) failed - see %s", len(failures), report)
        return 1
    report.write_text("All steps completed successfully.\n")
    LOG.info("pipeline complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
