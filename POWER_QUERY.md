# Wiring this into your Excel model

Two options. Use the CSV route unless you specifically want the formatted workbook.

## Option A - link to the CSVs (recommended)

The CSVs are the stable contract: columns never change, only rows get appended.
Replace `YOURNAME/amkr-nowcast` with your repo path.

In Excel: **Data > Get Data > From Other Sources > Blank Query > Advanced Editor**,
then paste:

```m
let
    Url     = "https://raw.githubusercontent.com/YOURNAME/amkr-nowcast/main/data/master_monthly.csv",
    Source  = Csv.Document(Web.Contents(Url), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Typed   = Table.TransformColumnTypes(Headers, {
                  {"period",    type text},
                  {"series_id", type text},
                  {"value",     type number},
                  {"unit",      type text},
                  {"freq",      type text},
                  {"source",    type text},
                  {"retrieved", type date}
              })
in
    Typed
```

Repeat for whichever of these you need, changing only the file name:

| File | What it holds |
|---|---|
| `data/master_monthly.csv`   | every monthly series, tidy long |
| `data/master_wide.csv`      | monthly levels pivoted, one column per series |
| `data/master_quarterly.csv` | calendar quarters with YoY and QoQ |
| `data/qtd.csv`              | the quarter being nowcast, months reported so far |
| `output/scenarios.csv`      | bull / base / bear vs guidance |
| `output/lead_lag.csv`       | correlation and implied lead per series |

Then **Data > Refresh All**, or tick *Refresh data when opening the file* under
Query Properties so the model is current every time you open it.

## Option B - link to the workbook

Point Power Query at
`https://raw.githubusercontent.com/YOURNAME/amkr-nowcast/main/output/amkr_indicator_data.xlsx`
using `Excel.Workbook(Web.Contents(Url))` and pick the sheet you want. Same
stable sheet and column names. Slightly heavier, but you get the number
formatting for free.

## Building a lookup against the tidy table

The long format is deliberate - it means adding a new series never breaks your
formulas. To pull a single value:

```excel
=SUMIFS(Data_Monthly[value], Data_Monthly[series_id], "ASE", Data_Monthly[period], "2026-06")
```

`SUMIFS` rather than `INDEX/MATCH` because there is exactly one row per
period/series, so the sum is the value, and it returns 0 rather than #N/A when
a month has not been filed yet.

## If you later make the repo private

The anonymous `raw.githubusercontent.com` URLs stop resolving. You then need a
fine-grained read-only PAT and must supply it as a header:

```m
Source = Csv.Document(
    Web.Contents(Url, [Headers=[Authorization="Bearer " & PAT]]), ...)
```

Store `PAT` as a separate query or a workbook parameter - do not paste the token
inline into every query, and do not commit it. Note that tokens expire (90 days
by default), and when one does the refresh fails with an auth error rather than
anything self-explanatory.
