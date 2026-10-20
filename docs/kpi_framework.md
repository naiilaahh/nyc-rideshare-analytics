# KPI framework

One metric tree, one set of formulas, and three delivery channels that must agree.

## The metric tree

```
                              PLATFORM MARGIN                        (output · Finance)
                       = trips × avg fare × take rate
            ┌──────────────────────┼───────────────────────┐
         TRIPS                 AVG FARE                TAKE RATE        (outputs / levers)
   market trips × share     miles × fare per mile   1 − driver pay ÷ fare
            │                      │                       │
   ── service quality ──    ── trip mix ──            ── driver supply ──   (inputs)
   avg wait                 airport trip share        earnings per engaged hour
   % picked up < 5 min      congestion-zone share     tip rate
   WAV fulfilment           avg trip miles
   shared match rate
```

**How to read it:** executives manage the outputs; operating teams own the inputs. A change in
platform margin breaks down exactly into a trips effect, an average-fare effect and a
take-rate effect (`notebooks/02_kpi_framework.ipynb`, section 3). Each of those links to the
inputs below it.

## Why components, not ratios, are stored

Gold tables store only **additive components** (sums and counts). Every ratio is computed
*after* aggregation, from the components, at whatever grain the user picks. That's why:

- Monthly take rate equals the take rate computed from daily rows, not an average of daily rates.
- Median wait isn't a KPI: medians can't be re-aggregated across zones or days. The additive
  **"% picked up within 5 minutes"** is used instead. A median appears only on the monthly exec
  table, labelled as non-additive.

## Components (19)

| Component | Meaning |
|---|---|
| `trips` | Completed trips (all, including DQ-flagged) |
| `trips_valid_econ` | Trips that pass the economics rules (DQ-03, DQ-04) |
| `base_fare_sum` | Base passenger fare, before tolls, taxes, fees and tips |
| `driver_pay_sum` | Driver pay excluding tips (TLC definition) |
| `tips_sum` | Tips |
| `rider_total_sum` | Everything the rider paid, tips included |
| `trip_miles_sum` | Trip miles |
| `trip_seconds_sum` | Engaged (on-trip) seconds |
| `wait_trips` | Trips with a valid request-to-pickup time (DQ-02) |
| `wait_seconds_sum` | Request-to-pickup seconds |
| `pickups_within_5min` | Pickups within 5 minutes of request |
| `shared_requests` | Trips where the rider asked to share |
| `shared_matches` | Shared requests that were matched |
| `wav_requests` | Wheelchair-accessible vehicle requests |
| `wav_matches` | WAV requests served by a WAV |
| `cbd_fee_sum` | Congestion relief zone fee collected (from 5 Jan 2025) |
| `cbd_fee_trips` | Trips charged the congestion relief zone fee |
| `crz_trips` | Trips starting or ending in the congestion relief zone |
| `airport_trips` | Trips starting or ending at JFK, LGA or EWR |

## KPIs (17)

| KPI | Label | Level | Formula (over components) | Owner |
|---|---|---|---|---|
| `trips` | Trips | output | `trips` | GM, NYC Market |
| `gross_bookings` | Gross Bookings | output | `rider_total_sum - tips_sum` | Finance, NYC Market |
| `platform_margin` | Platform Margin (fare - driver pay) | output | `base_fare_sum - driver_pay_sum` | Finance, NYC Market |
| `market_share` | Market Share (trips) | output | `trips` | GM, NYC Market |
| `avg_fare` | Average Base Fare | input | `(base_fare_sum) / (trips_valid_econ)` | Pricing |
| `fare_per_mile` | Fare per Mile | input | `(base_fare_sum) / (trip_miles_sum)` | Pricing |
| `avg_trip_miles` | Average Trip Miles | input | `(trip_miles_sum) / (trips_valid_econ)` | Pricing |
| `take_rate` | Take Rate | input | `(base_fare_sum - driver_pay_sum) / (base_fare_sum)` | Finance, NYC Market |
| `avg_wait_min` | Average Wait (min) | input | `(wait_seconds_sum / 60) / (wait_trips)` | Marketplace Ops |
| `pickup_within_5min_rate` | Picked Up Within 5 Min | input | `(pickups_within_5min) / (wait_trips)` | Marketplace Ops |
| `shared_match_rate` | Shared Ride Match Rate | input | `(shared_matches) / (shared_requests)` | Marketplace Ops |
| `wav_fulfillment_rate` | WAV Fulfillment Rate | input | `(wav_matches) / (wav_requests)` | Accessibility Program |
| `driver_earnings_per_engaged_hour` | Driver Earnings per Engaged Hour | input | `(driver_pay_sum + tips_sum) / (trip_seconds_sum / 3600)` | Driver Experience |
| `tip_rate` | Tip Rate | input | `(tips_sum) / (base_fare_sum)` | Driver Experience |
| `crz_trip_share` | Congestion-Zone Trip Share | input | `(crz_trips) / (trips)` | Policy & Strategy |
| `airport_trip_share` | Airport Trip Share | input | `(airport_trips) / (trips)` | Pricing |
| `cbd_fee_revenue` | Congestion Fee Collected | output | `cbd_fee_sum` | Policy & Strategy |

## Channel mapping: one definition, three tools

| KPI engine output | Executive (notebook 00; Tableau / Power BI optional) | Analyst (DuckDB / Databricks) | Embedded ops portal |
|---|---|---|---|
| Source table | `daily_zone_company.csv`, `exec_monthly_company.csv` | `gold_*` tables, Delta in Databricks | `hourly_zone_company.parquet` |
| How a KPI is computed | Notebook: `kpi_sql()`. Tableau: calculated field from `kpi_tableau()`. Power BI: measure from `kpi_dax()` | `kpi_sql()` inside any GROUP BY | `compute_kpis()` (pandas) |
| Example: take rate | `(SUM([base_fare_sum]) - SUM([driver_pay_sum])) / ZN(SUM([base_fare_sum]))` | `(sum(base_fare_sum) - sum(driver_pay_sum)) / nullif(sum(base_fare_sum), 0)` | `compute_kpis(df, names=['take_rate'])` |
| Example: market share | `(SUM([trips])) / TOTAL(SUM([trips]))` | window over company | `share_of_total` across companies |
| Guarantee | Checked against the warehouse (check B) | Reference (check A); independent SQL (check D) | Checked against the warehouse (check C) |

Every formula for every tool is in `data/exports/kpi_definitions.csv`, generated on each build.
Adding a KPI means adding one entry to `metrics/kpis.yml`; every channel picks it up.
