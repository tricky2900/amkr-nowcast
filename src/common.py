"""Shared plumbing: paths, the tidy schema, validation, and period math.

Every fetcher emits the SAME long/tidy frame so that adding a new source never
requires touching the analysis layer:

    period      str   'YYYY-MM' for monthly, 'YYYYQn' for quarterly
    series_id   str   matches config/series.yaml
    value       float raw level in the series' native unit
    unit        str   e.g. TWD_THOUSANDS, USD_MILLIONS
    freq        str   'M' or 'Q'
    source      str   provenance string, kept so a bad number is traceable
    retrieved   str   ISO date the row was pulled
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "series.yaml"
DATA = ROOT / "data"
RAW = DATA / "raw"
MANUAL = DATA / "manual"
OUTPUT = ROOT / "output"

COLUMNS = ["period", "series_id", "value", "unit", "freq", "source", "retrieved"]

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
)


def log(name: str) -> logging.Logger:
    return logging.getLogger(name)


def load_config() -> dict:
    with open(CONFIG) as fh:
        return yaml.safe_load(fh)


def today() -> str:
    return dt.date.today().isoformat()


# --------------------------------------------------------------------------
# tidy frame helpers
# --------------------------------------------------------------------------
def tidy(rows: list[dict]) -> pd.DataFrame:
    """Build a schema-conformant frame and fail loudly if a fetcher drifted."""
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"fetcher returned frame missing columns: {sorted(missing)}")
    df = df[COLUMNS].copy()
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna(subset=["value"]).sort_values(["series_id", "period"])


def save_raw(df: pd.DataFrame, name: str) -> Path:
    """Write a source's tidy output. Idempotent: re-running a month overwrites
    that month rather than appending a duplicate."""
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / f"{name}.csv"
    if path.exists():
        prior = pd.read_csv(path)
        keys = set(zip(df["period"], df["series_id"]))
        prior = prior[~prior.apply(lambda r: (r["period"], r["series_id"]) in keys, axis=1)]
        df = pd.concat([prior, df], ignore_index=True)
    df = df[COLUMNS].sort_values(["series_id", "period"])
    df.to_csv(path, index=False)
    return path


def read_raw(name: str) -> pd.DataFrame:
    path = RAW / f"{name}.csv"
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(path)


# --------------------------------------------------------------------------
# period math
# --------------------------------------------------------------------------
def month_to_quarter(period: str) -> str:
    y, m = period.split("-")
    return f"{y}Q{(int(m) - 1) // 3 + 1}"


def quarter_sort_key(q: str) -> tuple[int, int]:
    y, n = q.split("Q")
    return int(y), int(n)


def next_quarter(q: str) -> str:
    y, n = quarter_sort_key(q)
    return f"{y + 1}Q1" if n == 4 else f"{y}Q{n + 1}"


def prior_year_quarter(q: str) -> str:
    y, n = quarter_sort_key(q)
    return f"{y - 1}Q{n}"


def months_in_quarter(q: str) -> list[str]:
    y, n = q.split("Q")
    start = (int(n) - 1) * 3 + 1
    return [f"{y}-{m:02d}" for m in range(start, start + 3)]


def shift_month(period: str, k: int) -> str:
    """Shift 'YYYY-MM' by k months (k may be negative)."""
    y, m = (int(x) for x in period.split("-"))
    total = y * 12 + (m - 1) + k
    return f"{total // 12}-{total % 12 + 1:02d}"


def to_quarterly(
    df: pd.DataFrame, series_id: str, lead_months: int = 0, require_complete: bool = True
) -> pd.DataFrame:
    """Aggregate one monthly series into calendar quarters.

    lead_months shifts the series FORWARD in time before aggregation, so
    lead_months=1 tests 'does this month's peer revenue explain AMKR one month
    later'. Returns columns [quarter, value, n_months].
    """
    s = df[(df.series_id == series_id) & (df.freq == "M")].copy()
    if s.empty:
        return pd.DataFrame(columns=["quarter", "value", "n_months"])
    s["period"] = s["period"].map(lambda p: shift_month(p, lead_months))
    s["quarter"] = s["period"].map(month_to_quarter)
    out = s.groupby("quarter")["value"].agg(["sum", "count"]).reset_index()
    out.columns = ["quarter", "value", "n_months"]
    if require_complete:
        out = out[out.n_months == 3]
    return out.sort_values("quarter", key=lambda c: c.map(quarter_sort_key))


def yoy(frame: pd.DataFrame, period_col: str, value_col: str = "value") -> pd.DataFrame:
    """Year-over-year growth. Works for monthly (lag 12) or quarterly (lag 4)."""
    f = frame.copy()
    lag = 12 if "-" in str(f[period_col].iloc[0]) else 4
    f = f.sort_values(period_col, key=lambda c: c.map(
        quarter_sort_key if lag == 4 else (lambda p: tuple(int(x) for x in p.split("-")))
    ))
    f["yoy"] = f[value_col] / f[value_col].shift(lag) - 1
    return f


def validate(df: pd.DataFrame, name: str, expect_series: list[str]) -> list[str]:
    """Return a list of human-readable warnings. Used by the Actions run to
    decide whether to open an alert issue instead of silently committing junk."""
    warns: list[str] = []
    if df.empty:
        return [f"{name}: fetched zero rows"]
    got = set(df.series_id.unique())
    for sid in expect_series:
        if sid not in got:
            warns.append(f"{name}: series {sid} returned no data")
    for sid, grp in df.groupby("series_id"):
        v = pd.to_numeric(grp["value"], errors="coerce").dropna()
        if (v <= 0).any():
            warns.append(f"{name}: {sid} has non-positive values")
        if len(v) > 13:
            recent, hist = v.iloc[-1], v.iloc[-13:-1]
            if hist.median() and abs(recent / hist.median() - 1) > 3:
                warns.append(
                    f"{name}: {sid} latest value is >4x its trailing median "
                    f"({recent:,.0f}) - possible unit change or parse error"
                )
    return warns
