# Data-quality rules

Each rule came from a finding in `notebooks/01_data_discovery.ipynb`, is implemented as a flag
in `sql/silver_trips.sql` (and mirrored in `notebooks/05_databricks_pyspark.ipynb`), and is
referenced by the KPIs it affects in `metrics/kpis.yml`.

**Principle:** flag, don't delete. Every completed trip stays in the silver layer, so trip
totals match TLC's published counts. Each KPI then uses only the rows that are valid *for
that KPI*.

| Rule | Finding (Oct 2024 – Mar 2025) | Treatment | Flag | KPIs affected |
|---|---|---|---|---|
| **DQ-01** Unknown zone | ~4.5% of trips have zone 264 (Unknown) or 265 (Outside NYC) | Kept in totals; excluded from zone views, the ops portal and anomaly scoring | `is_unknown_pu_zone` | Zone-level views |
| **DQ-02** Invalid wait | ~1% of trips (1.7% of Lyft) have a request time *after* pickup; a handful wait over 2 h | Excluded from wait KPIs only | `is_valid_wait` | avg_wait_min, pickup_within_5min_rate |
| **DQ-03** Invalid fare | Zero or negative fares; Lyft spikes in Dec (31k), Jan (14k) and Mar (63k); a few fares over $1,000 | Excluded from price and margin KPIs; still counted as trips | `is_valid_economics` | avg_fare, fare_per_mile, take_rate, platform_margin, gross_bookings, earnings |
| **DQ-04** Invalid distance or duration | Zero miles, over 150 miles, or trips over 4 h. Timestamp durations are wrong across DST changes (3 Nov 2024, 9 Mar 2025). | Durations always use TLC's `trip_time`, never `dropoff - pickup`; out-of-range trips are excluded from economics | `is_valid_economics` | fare_per_mile, avg_trip_miles, earnings per engaged hour |
| **DQ-05** Driver pay above fare | Driver pay exceeds fare + tips + $5 on 2–5% of Uber trips and ~1% of Lyft | **Kept**: real money, likely incentives. Dropping them would overstate take rate. | `is_pay_above_fare` | take_rate (can be negative in a slice) |
| **DQ-06** Company-specific gaps | Lyft leaves `on_scene_datetime` and `originating_base_num` empty on 99.7% of trips, until it starts sending driver arrival in March 2025 | No cross-company KPI uses driver-arrival time; service is measured request-to-pickup | n/a | Design decision |
| **DQ-07** Month attribution | Monthly files can contain trips from adjacent months | Trips are attributed by pickup date and filtered to the reporting window | n/a | All monthly KPIs |
| **DQ-08** Schema drift | `cbd_congestion_fee` exists only from January 2025 | Read by column name (`union_by_name` / `mergeSchema`); missing = 0 | n/a | cbd_fee_revenue |

## Validating the congestion-zone definition

`reference/zone_attributes.csv` lists the 38 taxi zones south of 60th Street. This list is
checked against the real fee data in `crz_zone_validation`. For trips from a Manhattan zone to
another borough after 5 January 2025:

- Zones on the list were charged the fee on **88–97%** of trips.
- Zones just north of 60th St (Lincoln Square, Lenox Hill West, Upper East Side South, Central
  Park) were charged on **53–73%** of trips. Those riders pass *through* the zone on their way
  to Brooklyn or Queens.

So the list is correct, and through-trips are a known source of contamination in the
congestion-pricing control group.

## Automated checks

| Check | Where | Status |
|---|---|---|
| Every KPI in every channel matches the warehouse | `scripts/02_check_consistency.py` | 532 / 532 pass |
| Hand-written SQL from silver matches the generated KPI SQL (catches formula bugs) | same script, check D | 84 / 84 pass |
| Spark and DuckDB gold tables agree | last cell of notebook 05 | Runs when Spark is available |
| KPIs only reference additive components, using + − × ÷ | `metrics/kpi.py` validation on load | Enforced |
