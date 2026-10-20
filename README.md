# NYC Rideshare Analytics Suite

**One analytics backend serving three audiences: executives, analysts and front-line ops
managers. It's built on 121 million real Uber and Lyft trips, and every KPI is defined once and
proven identical across all channels.**

> Independent portfolio project using public [NYC TLC trip record data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)
> (High-Volume For-Hire Vehicle records, October 2024 – March 2025). Not affiliated with Uber, Lyft or the TLC.

---

## The scenario

I played the **Analytics Product Owner for a rideshare company's NYC market**. The market team
had three problems:

1. Executives got conflicting numbers from hand-built decks.
2. Analysts re-cleaned raw trip files for every question.
3. Ops managers learned about demand surges after riders complained.

And one urgent question: **what did Manhattan congestion pricing (from 5 January 2025) do to
the business?**

## What I built

| Persona | Channel | Deliverable |
|---|---|---|
| **Executives** | Executive dashboard notebook (Tableau / Power BI extracts included) | [`notebooks/00_executive_dashboard.ipynb`](notebooks/00_executive_dashboard.ipynb): KPI tiles, market share, economics, service quality, zone treemap, congestion pricing |
| **Analysts / data scientists** | Curated DuckDB warehouse + Databricks/PySpark pipeline + analysis notebooks | Notebooks [`01`](notebooks/01_data_discovery.ipynb)–[`05`](notebooks/05_databricks_pyspark.ipynb), silver/gold layers |
| **Embedded ops users** | Driver Ops Portal (Streamlit), isolated per company | [`app/streamlit_app.py`](app/streamlit_app.py): live-replay zone KPIs, surge/drop alerts, one-click actions with an audit log |

Product documents: [PRD and roadmap](docs/PRD.md) · [KPI framework](docs/kpi_framework.md) · [Data-quality rules](docs/data_quality_rules.md)

## Architecture

```
 NYC TLC CDN ──► bronze  data/raw/*.parquet              6 monthly files, 121.4M trips, schema drift
                   │
                   ▼  sql/silver_trips.sql  (DQ-01..08: flag, don't delete)
                 silver  data/silver/trips/pickup_ym=*/    typed, enriched, quality-flagged
                   │
                   ▼  aggregate SQL generated from metrics/kpis.yml (identical in DuckDB and Spark SQL)
                 gold    data/warehouse.duckdb            additive components at hour / day / week / month
                   │                                      + zone-hour anomaly scores
      ┌────────────┼──────────────────────┐
      ▼            ▼                      ▼
  Executive     Analyst notebooks      Driver Ops Portal
  dashboard     DuckDB · PySpark       (Streamlit, tenant-isolated)
      └────────────┴──────────┬───────────┘
                              ▼
               metrics/kpi.py: ONE engine computes every KPI
               scripts/02_check_consistency.py: proves all channels agree (532/532)
```

## Results (Oct 2024 → Mar 2025)

| | Uber | Lyft |
|---|---|---|
| Trips (6 months) | 90.5M | 30.8M |
| Market share | 75.6% → 70.8% | 24.4% → **29.2%** |
| Take rate | 23.4% → **28.1%** | 20.9% → 21.7% |
| Picked up within 5 min | 59–72% | 54–66% |
| Driver earnings per engaged hour | $64–68 | $62–65 |

The congestion-pricing difference-in-differences, the margin bridge and the anomaly tuning are
in notebooks 02–04.

## Decisions worth asking me about

- **Data discovery changed the KPI design.** Lyft didn't report driver-arrival time until March
  2025, so "wait time" is request-to-pickup for both companies. Daylight saving breaks
  timestamp durations, so durations use TLC's own `trip_time`.
- **Store components, compute ratios late.** Gold tables hold only sums and counts. Take rate
  for a month is computed from that month's totals, never averaged from daily rates. That's
  also why the service KPI is "% picked up within 5 minutes" (additive) rather than median wait.
- **Consistency is tested, not promised.** Every KPI is computed four ways (warehouse SQL,
  Tableau extract, app feed, and hand-written SQL from silver). The build fails on any
  mismatch.
- **I validated my own assumption.** The congestion-zone list was checked against the real fee
  data. That showed through-traffic from zones north of 60th St paying the fee, a caveat I
  carried into the impact estimate.
- **Alert fatigue as a product metric.** Notebook 04 sets the alert threshold by "alerts per
  manager per shift", not by statistics alone. The portal exposes the threshold, and the action
  log measures alert precision.

## Run it

Needs Python 3.11+ and about 4 GB of free disk space.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/00_get_and_profile_data.py    # download + profile ~3 GB of TLC data (5–15 min)
python scripts/01_build_warehouse.py         # bronze → silver → gold (~1 min on a laptop)
python scripts/02_check_consistency.py       # prove every channel agrees

streamlit run app/streamlit_app.py           # Driver Ops Portal
```

Then open the notebooks in VS Code or Jupyter, choose the `.venv` kernel, and **Run All**,
starting with `00_executive_dashboard.ipynb`.

**Quick try without the full download:** run `scripts/00_get_and_profile_data.py --start 2024-10 --end 2024-10`,
then `scripts/01_build_warehouse.py --sample`.

### Databricks (notebook 05)

1. Create a free account at [Databricks Free Edition](https://www.databricks.com/learn/free-edition).
2. **Workspace → Create → Git folder**, and paste this repo's URL.
3. **Catalog → Create volume** (for example `main.nyc_tlc.raw`). Upload the files from
   `data/raw/` and `reference/zone_attributes.csv`.
4. Open `notebooks/05_databricks_pyspark.ipynb`, check `RAW_PATH` and `CATALOG_SCHEMA` in the
   first cell, and **Run all**. Silver and gold are written as Delta tables.

### Tableau / Power BI (optional)

`data/exports/` contains `daily_zone_company.csv`, `exec_monthly_company.csv` and
`weekly_crz_company.csv`. `kpi_definitions.csv` lists the exact Tableau calculated field and
Power BI DAX measure for every KPI, generated from the same definitions as everything else.

## Repository map

```
├── notebooks/        00 executive dashboard · 01 discovery · 02 KPI framework
│                     03 congestion pricing · 04 demand anomalies · 05 Databricks/PySpark
├── app/              Streamlit Driver Ops Portal
├── metrics/          kpis.yml (the single source of truth) + kpi.py (shared engine)
├── sql/              silver_trips.sql (DQ rules)
├── scripts/          00 download/profile · 01 build warehouse · 02 consistency check
├── reference/        congestion-zone and airport zone list
├── docs/             PRD · KPI framework · data-quality rules
└── data/             profile/ and small exports are committed; raw, silver and warehouse are regenerated
```

## Tech

Python · DuckDB · SQL · PySpark / Databricks (Delta) · pandas · Plotly · Streamlit · Jupyter ·
Parquet · Tableau / Power BI–ready extracts
