# AMKR Nowcast

Monthly Taiwan semiconductor indicators, correlated against Amkor Technology's
quarterly revenue, to produce a guidance-anchored bull/base/bear estimate for
the quarter in progress.

The point: ASE, KYEC, ChipMOS and Powertech report revenue **monthly**, Amkor
reports **quarterly**. By the time Amkor prints, two or three months of its
peers' books are already public. This repo turns that timing gap into an
estimate, weeks before the release.

---

## Quick start

```bash
git clone https://github.com/YOURNAME/amkr-nowcast.git
cd amkr-nowcast
pip install -r requirements.txt

python run_all.py            # first run backfills ~5 years, takes a few minutes
```

Then check `output/`:

| File | What it is |
|---|---|
| `amkr_indicator_data.xlsx` | the workbook your Excel model links to |
| `scenarios.csv` | bull / base / bear vs guidance for the quarter in progress |
| `lead_lag.csv` | which series lead Amkor, and by how much |
| `model_ranking.csv` | every predictor ranked by **backtest** error, not in-sample fit |
| `chart_*.png` | YoY comparison, lead/lag, backtest, scenario range |
| `run_status.txt` | empty-ish on success, lists failures otherwise |

To wire it into Excel, see [POWER_QUERY.md](POWER_QUERY.md).

### Automating it

Push to GitHub and the workflow in `.github/workflows/monthly-refresh.yml` runs
on the **12th of each month** — after MOF exports (~8th) and the Taiwan filing
deadline (10th). It commits the refreshed data and opens an issue if a source
breaks, so a silently stale database is not a failure mode.

Two things it cannot do on its own, because neither has a stable machine-readable
feed: SIA regional billings and Amkor's guidance range. See
[Manual inputs](#manual-inputs).

---

## What gets tracked

**Taiwan monthly revenue** (TWSE filings, due by the 10th)
TSMC 2330 · ASE 3711 · KYEC 2449 · ChipMOS 8150 · Powertech 6239

**SIA regional billings** — Global, Americas, Europe, Japan, China, APAC/Other

**Taiwan exports** (MOF, ~8th) — Total · Machinery & Electrical Equipment ·
Electronic Product Parts · Machinery · Electrical Machinery Products ·
ICT & Audio-Video · Domestic Appliances · Electronic + ICT (derived)

**Target** — AMKR quarterly net sales, from SEC EDGAR XBRL

**Derived** — `OSAT_COMPOSITE`, a revenue-weighted index of the four OSATs

Edit `config/series.yaml` to add or drop series. Nothing else needs changing.

---

## Method

### Everything is in YoY space, on purpose

Correlating raw revenue *levels* across these series gives r > 0.95 for almost
any pair — they all trend with the cycle and with inflation. That number is
worthless: it measures shared trend, not shared information. Year-over-year
differencing strips the trend and leaves the cyclical signal that actually
forecasts. Every correlation and regression here operates on YoY.

### Lead/lag screening

Each monthly series is tested against Amkor's quarterly YoY at 0, 1, 2 and 3
month leads, and the best lead per series is reported. Rough priors, worth
re-checking against your own output: TSMC tends to lead by about a month, the
OSATs are close to coincident, and SIA and the export data run
coincident-to-lagging.

### Model selection by backtest, not by fit

Predictors are screened **univariately**. A kitchen-sink regression on these
inputs produces unstable, sign-flipping coefficients, because the predictors
are all reading the same cycle and are heavily collinear with each other.

The ranking uses **rolling-origin backtest RMSE** — refitting at each step on
only the data that existed at the time, so nothing leaks backwards. In-sample
R² is reported alongside, and it is usually far more flattering. Trust the
backtest number; it is the one that sets the scenario bands.

### Matched-horizon refit

The naive approach fits on complete quarters and then feeds the model two
months of data, which quietly understates the error — two months is a noisier
read on a quarter than three. Instead, when only *n* months of the current
quarter have printed, the model is **refit on the first *n* months of every
historical quarter**. The two-month model is genuinely worse than the
three-month one, and this makes that visible in the bands rather than hiding it.

### Guidance-anchored scenarios

```
base = model point estimate
bull = max(guidance high, model + z·sigma)
bear = min(guidance low,  model − z·sigma)
```

The band never claims more precision than Amkor's own outlook, but widens when
the Taiwan data disagrees with it. The number worth acting on is
**`delta_vs_guide_mid_pct`** — how far the peer data implies Amkor lands from
its own midpoint. With no guidance on file, it falls back to pure model
percentiles and says so in the `anchor` column.

`z` is set in `config/series.yaml` (default 0.84 ≈ a 20th/80th percentile band).

---

## Manual inputs

Two files in `data/manual/` need human or Claude-assisted upkeep. Both are
plain CSV so every revision shows up as a git diff.

**`amkr_guidance.csv`** — seeded with 1Q26 and 2Q26 from Amkor's 8-Ks. Add one
row per quarter when guidance is issued (with the quarterly results, so about
four times a year). Backfilling older quarters is optional but improves the
model-vs-guidance comparison.

**`sia_billings.csv`** — ships with a clearly marked example row that the loader
ignores and you should delete. SIA publishes on roughly a five-week lag.

> **Important:** SIA/WSTS monthly figures are **three-month moving averages**,
> not discrete months. They are smoothed, so they lag turning points and their
> quarterly sums are not true quarterly billings. Treat them as confirmation,
> never as a leading signal.

`data/manual/taiwan_exports.csv` is a fallback, only needed if the MOF API path
breaks (see below).

Both files are structured so a monthly Claude/Cowork task can append to them —
see [`skills/refresh-amkr-nowcast/SKILL.md`](skills/refresh-amkr-nowcast/SKILL.md).

---

## Known weaknesses

Read this section before you trade on the output.

**Five years is 20 quarters.** That is a small sample for a regression, and it
spans an unusually violent cycle. Treat coefficients as indicative and prefer
wider bands. Raise `history_years` in the config for more degrees of freedom.

**The MOF export fetcher is the fragile one.** MOF publishes through an
interactive portal, not a documented REST API, and its category codes are not
stable across site revisions. `src/fetch_mof.py` targets the portal's JSON
endpoint and falls back to a manual CSV rather than pretending success. This is
the module most likely to need attention on first run — verify its output before
relying on the export series.

**ASE's monthly number includes USI's EMS business,** which is not OSAT and
dilutes the signal. This is why `OSAT_COMPOSITE` caps any single member at 45%
(`max_weight` in the config) — uncapped revenue weighting puts ASE at ~82% and
the composite becomes an ASE proxy.

**Mix differs.** ChipMOS and Powertech skew memory and display drivers; Amkor
skews advanced SiP, communications and automotive. Correlation should be good
but never perfect, and divergence is often informative rather than an error.

**Currency.** Taiwan files in TWD, Amkor reports in USD. Working in YoY growth
sidesteps most of this, but a sharp TWD move will still distort the comparison.

**Restatements.** The AMKR fetcher keeps the earliest-filed value for each
quarter, so a restatement appears as a git diff rather than silently rewriting
history.

---

## Layout

```
config/series.yaml          what gets tracked; edit here, not in code
src/
  common.py                 tidy schema, period math, validation
  fetch_twse.py             Taiwan monthly revenue (FinMind > TWSE > MOPS)
  fetch_mof.py              Taiwan exports (fragile; CSV fallback)
  fetch_amkr.py             AMKR quarterly from SEC EDGAR, Q4 derived
  manual_sources.py         SIA + guidance CSV loaders
  build_master.py           consolidation, OSAT composite, nowcast target
  analyze.py                lead/lag screen, model selection, backtest
  scenarios.py              guidance-anchored bull/base/bear
  make_excel.py             the workbook your model links to
  make_charts.py            PNGs
run_all.py                  orchestrator (--no-fetch, --only-fetch)
tests/synthetic_check.py    end-to-end validation on data with a known answer
```

### Testing

```bash
python tests/synthetic_check.py
```

Builds a synthetic world where peers lead Amkor **by construction**, runs the
full pipeline, and asserts the analysis recovers the relationship it was given:
correct nowcast quarter, positive sane beta, TSMC detected as leading, correct
scenario ordering, guidance anchoring applied. It does not test the fetchers —
those need live internet — but everything downstream of them is covered. If it
prints FAIL, the analysis layer is broken regardless of what the real data says.

Note that it overwrites `data/raw/` with synthetic values, so run a real
`python run_all.py` afterwards before looking at any output.
