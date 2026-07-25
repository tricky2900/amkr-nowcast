---
name: refresh-amkr-nowcast
description: "Use this skill to run the monthly AMKR nowcast refresh - updating the SIA regional billings and Amkor guidance CSVs, rerunning the pipeline, and reporting the bull/base/bear estimate against guidance. Triggers on 'refresh the AMKR nowcast', 'update the Taiwan indicator data', 'run the monthly nowcast refresh', 'what does the Taiwan data say about Amkor', or any request to update or review the amkr-nowcast repo. Also use when the user asks for a pre-earnings read on Amkor built from OSAT peer monthly revenue."
---

# Monthly AMKR nowcast refresh

GitHub Actions already handles the scriptable sources (Taiwan monthly revenue,
exports, AMKR actuals from EDGAR). This skill covers the two inputs that need
judgment, plus the review pass.

## 1. Check what the automation already did

Look at the most recent Actions run and at `output/run_status.txt`. If an alert
issue is open, deal with that first — a broken fetcher means the numbers below
are stale, and reporting a confident nowcast off stale data is the main way this
system can mislead.

## 2. Update SIA regional billings

Find the latest SIA/WSTS monthly release (semiconductors.org, or the WSTS
figures reported in trade press). You need six numbers: Global, Americas,
Europe, Japan, China, APAC/Other, in USD millions.

Append one row per month to `data/manual/sia_billings.csv`. Do not overwrite
existing rows. If the example row starting with `EXAMPLE` is still present,
delete it.

Two things to hold onto:
- SIA publishes on roughly a five-week lag, so the newest month available will
  be older than the Taiwan data. That is expected, not an error.
- **These are three-month moving averages, not discrete months.** Never present
  them as monthly prints, and do not treat them as a leading indicator.

If you cannot find a figure with confidence, leave the cell blank rather than
estimating it. A gap is handled cleanly by the loader; a fabricated number is
not recoverable once it is in the history.

## 3. Update Amkor guidance

Guidance is issued with quarterly results, so this changes about four times a
year. Check whether a new quarter's guidance exists that is not yet in
`data/manual/amkr_guidance.csv`.

The source of record is the earnings 8-K on SEC EDGAR or the release on
ir.amkor.com — take the net sales range from the "Business Outlook" section.
Always record the source URL in the row. Do not take the range from a news
summary or an aggregator; those paraphrase and occasionally garble the figures.

## 4. Rerun and review

```bash
python run_all.py --no-fetch     # if Actions already pulled the scriptable sources
python run_all.py                # otherwise, full refresh
```

Then read `output/scenarios.csv` and report:

- The quarter being nowcast, and **how many of the three months of peer data
  are in**. A one-month read is much weaker than a three-month read, and the
  report should say so plainly rather than quoting a point estimate as if the
  horizon did not matter.
- Base case, and the bear/bull range.
- **`delta_vs_guide_mid_pct`** — the actual signal. How far the Taiwan data
  implies Amkor lands from its own guidance midpoint.
- Which predictor was selected, its backtest RMSE, and whether the top two or
  three predictors agree. Disagreement between them is a warning that the
  estimate is fragile, and is worth flagging explicitly.

## 5. Sanity checks before reporting

- Does any series show a sudden order-of-magnitude jump? That is usually a unit
  change or a parse error, not a real move. `output/run_status.txt` and the
  validation warnings in `data/raw/twse_warnings.txt` flag the obvious cases.
- Did AMKR report since the last run? If so the nowcast target should have
  advanced by one quarter. If it has not, `fetch_amkr.py` may have failed.
- Compare the realised result against what the model said last quarter. Log the
  miss. A model that is quietly drifting is worth catching early, and the
  backtest will not show recent degradation on its own.

## Reporting tone

Give the estimate with its uncertainty attached, not as a single number. The
useful output is "the peer data implies roughly X, which is N% below/above the
guidance midpoint, on a two-month read with a backtest RMSE of Y points" — not
"Amkor will report X". Where the model and guidance disagree materially, say so
and note that mix differences (ChipMOS/PTI skew memory; Amkor skews advanced
SiP, comms and auto) are a plausible benign explanation.
