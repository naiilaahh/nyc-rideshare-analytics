"""
Step 1: build the analytics warehouse (bronze -> silver -> gold) with DuckDB.

    python scripts/01_build_warehouse.py            # full build from data/raw/*.parquet
    python scripts/01_build_warehouse.py --sample   # 30-second smoke test on data/sample/fhvhv_sample.csv

Layers
  bronze  data/raw/fhvhv_tripdata_YYYY-MM.parquet      (TLC files, untouched)
  silver  data/silver/trips/pickup_ym=YYYY-MM/*.parquet (typed, enriched, DQ-flagged; sql/silver_trips.sql)
  gold    data/warehouse.duckdb                         (additive component tables from metrics/kpis.yml)
  serve   data/exports/                                 (Tableau / Power BI CSVs, app parquet, KPI definitions)
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from metrics.kpi import component_select_sql, kpi_dax, kpi_names, kpi_sql, kpi_tableau, spec  # noqa: E402

DATA = ROOT / "data"


def add_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{y + (m == 12):04d}-{m % 12 + 1:02d}"


def step(msg: str):
    print(f"  {time.strftime('%H:%M:%S')}  {msg}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="build from the 0.05%% sample CSV")
    args = ap.parse_args()
    sfx = "_sample" if args.sample else ""
    silver_dir = DATA / f"silver{sfx}" / "trips"
    exports = DATA / f"exports{sfx}"
    db_path = DATA / f"warehouse{sfx}.duckdb"
    t0 = time.time()

    # ------------------------------------------------------------ bronze
    raw_files = sorted((DATA / "raw").glob("fhvhv_tripdata_*.parquet"))
    if not raw_files:
        sys.exit("No raw files in data/raw. Run scripts/00_get_and_profile_data.py first.")
    months = [p.stem.split("_")[-1] for p in raw_files]
    start, end = f"{months[0]}-01", f"{add_month(months[-1])}-01"
    if args.sample:
        source = f"read_csv('{DATA}/sample/fhvhv_sample.csv', header = true, auto_detect = true)"
    else:
        source = f"read_parquet('{DATA}/raw/fhvhv_tripdata_*.parquet', union_by_name = true)"

    db_path.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute("set preserve_insertion_order = false")
    cols = {r[0] for r in con.execute(f"describe select * from {source}").fetchall()}
    cbd_fee = "cbd_congestion_fee" if "cbd_congestion_fee" in cols else "0"
    print(f"Building {'SAMPLE' if args.sample else 'FULL'} warehouse for {start} .. {end} (exclusive)")

    # ------------------------------------------------------------ reference
    step("dim_zone")
    con.execute(f"""
        create table dim_zone as
        select
            cast(z.LocationID as integer)              as zone_id,
            z.Borough                                  as borough,
            z.Zone                                     as zone,
            z.service_zone                             as service_zone,
            coalesce(a.is_crz, false)                  as is_crz,
            coalesce(a.is_airport, false)              as is_airport,
            cast(z.LocationID as integer) in (264, 265) as is_unknown
        from read_csv('{DATA}/raw/taxi_zone_lookup.csv', header = true) as z
        left join read_csv('{ROOT}/reference/zone_attributes.csv', header = true) as a
            on a.zone_id = cast(z.LocationID as integer)
    """)
    con.execute("create temp view zone_attributes as select zone_id, is_crz, is_airport from dim_zone")

    # ------------------------------------------------------------ silver
    step("silver: writing partitioned parquet (the slow step: a few minutes for 6 months)")
    shutil.rmtree(silver_dir, ignore_errors=True)
    silver_dir.mkdir(parents=True)
    silver_sql = (ROOT / "sql/silver_trips.sql").read_text().format(
        source=source, cbd_fee=cbd_fee, start=start, end=end)
    con.execute(f"copy ({silver_sql}) to '{silver_dir}' (format parquet, partition_by (pickup_ym))")
    con.execute(f"""
        create view silver_trips as
        select * from read_parquet('{silver_dir}/*/*.parquet', hive_partitioning = true)
    """)

    # ------------------------------------------------------------ gold (components only)
    comps = component_select_sql()
    gold = {
        "gold_hourly_zone_company": "company, pu_zone_id, pickup_date, pickup_hour, pickup_dow, pickup_hour_of_day",
        "gold_daily_zone_company": "company, pu_zone_id, pickup_date, pickup_week, pickup_month",
        "gold_weekly_crz_company": "company, pickup_week, touches_crz",
        "gold_monthly_company": "company, pickup_month",
    }
    for table, grain in gold.items():
        step(table)
        extra = ""
        if table == "gold_monthly_company":   # non-additive extras, exec view only (labelled as such)
            extra = (",\n    quantile_cont(wait_seconds, 0.5) filter (where is_valid_wait) / 60.0 as median_wait_min"
                     ",\n    quantile_cont(wait_seconds, 0.9) filter (where is_valid_wait) / 60.0 as p90_wait_min")
        con.execute(f"create table {table} as select {grain},\n    {comps}{extra}\nfrom silver_trips group by {grain}")

    # ------------------------------------------------------------ anomaly scoring
    step("gold_zone_hour_scored (demand anomalies vs same hour, last 4 weeks)")
    con.execute("""
        create table gold_zone_hour_scored as
        with hourly as (
            select * from gold_hourly_zone_company where pu_zone_id not in (264, 265)
        ),
        scored as (
            select
                *,
                avg(trips)         over w as baseline_trips,
                stddev_samp(trips) over w as baseline_std,
                count(*)           over w as baseline_weeks,
                avg(wait_seconds_sum / nullif(wait_trips, 0) / 60.0) over w as baseline_avg_wait_min
            from hourly
            window w as (
                partition by company, pu_zone_id, pickup_dow, pickup_hour_of_day
                order by pickup_hour
                range between interval 28 days preceding and interval 1 hour preceding
            )
        )
        select
            *,
            -- Poisson floor stops tiny sample variance from producing huge z-scores
            (trips - baseline_trips) / greatest(baseline_std, sqrt(baseline_trips), 1.0) as z_score,
            trips / nullif(baseline_trips, 0) - 1                                       as pct_vs_baseline,
            case
                when baseline_weeks < 4 then null
                when greatest(trips, baseline_trips) < 25 then null
                when (trips - baseline_trips) / greatest(baseline_std, sqrt(baseline_trips), 1.0) >= 3
                     and trips / nullif(baseline_trips, 0) - 1 >= 0.4  then 'surge'
                when (trips - baseline_trips) / greatest(baseline_std, sqrt(baseline_trips), 1.0) <= -3
                     and trips / nullif(baseline_trips, 0) - 1 <= -0.4 then 'drop'
            end as anomaly_type
        from scored
    """)

    # ------------------------------------------------------------ CRZ zone-list validation (2025 fee data)
    step("crz_zone_validation")
    con.execute("""
        create table crz_zone_validation as
        select
            s.pu_zone_id, z.zone, z.borough, z.is_crz,
            count(*)                                                          as trips_to_outer_boroughs,
            avg(case when s.cbd_congestion_fee > 0 then 1.0 else 0.0 end)     as pct_charged_cbd_fee
        from silver_trips as s
        join dim_zone as z  on z.zone_id = s.pu_zone_id
        join dim_zone as dz on dz.zone_id = s.do_zone_id
        where s.pickup_date >= date '2025-01-05'
          and z.borough = 'Manhattan'
          and dz.borough in ('Queens', 'Brooklyn', 'Bronx', 'Staten Island')
        group by 1, 2, 3, 4
        having count(*) >= 200
    """)

    # ------------------------------------------------------------ exports
    step("exports")
    shutil.rmtree(exports, ignore_errors=True)
    exports.mkdir(parents=True)
    zone_cols = "z.zone, z.borough, z.service_zone, z.is_crz, z.is_airport, z.is_unknown"
    con.execute(f"copy (select * from gold_monthly_company order by pickup_month, company) to '{exports}/exec_monthly_company.csv' (header)")
    con.execute(f"""copy (select g.*, {zone_cols} from gold_daily_zone_company g join dim_zone z on z.zone_id = g.pu_zone_id
                   order by pickup_date, company, pu_zone_id) to '{exports}/daily_zone_company.csv' (header)""")
    con.execute(f"copy (select * from gold_weekly_crz_company order by pickup_week, company, touches_crz) to '{exports}/weekly_crz_company.csv' (header)")
    con.execute(f"""copy (select s.*, {zone_cols} from gold_zone_hour_scored s join dim_zone z on z.zone_id = s.pu_zone_id
                   where anomaly_type is not null order by pickup_hour, company) to '{exports}/zone_hour_anomalies.csv' (header)""")
    con.execute(f"copy (select * from gold_zone_hour_scored) to '{exports}/hourly_zone_company.parquet' (format parquet)")
    con.execute(f"copy (select * from dim_zone) to '{exports}/dim_zone.parquet' (format parquet)")
    con.execute(f"copy (select * from crz_zone_validation order by is_crz desc, pct_charged_cbd_fee) to '{exports}/crz_zone_validation.csv' (header)")

    import csv
    with open(exports / "kpi_definitions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "label", "level", "type", "format", "owner", "definition", "tableau_formula", "dax_formula", "sql_over_gold"])
        for n in kpi_names():
            k = spec()["kpi_by_name"][n]
            w.writerow([n, k["label"], k["level"], k["type"], k["format"], k["owner"], k["definition"],
                        kpi_tableau(n), kpi_dax(n), kpi_sql(n)])

    # ------------------------------------------------------------ summary
    n_silver = con.execute("select count(*) from silver_trips").fetchone()[0]
    n_anom = con.execute("select count(*) from gold_zone_hour_scored where anomaly_type is not null").fetchone()[0]
    headline = con.execute(f"""
        select company,
               sum(trips)                         as trips,
               {kpi_sql('take_rate')}             as take_rate,
               {kpi_sql('avg_wait_min')}          as avg_wait_min,
               {kpi_sql('driver_earnings_per_engaged_hour')} as earn_per_hr
        from gold_monthly_company group by company order by trips desc
    """).fetchall()
    con.close()
    print(f"\nDone in {time.time() - t0:,.0f}s. silver rows: {n_silver:,}; anomalies flagged: {n_anom:,}")
    for c, t, tr, w, e in headline:
        print(f"  {c:<5} trips {t:>12,}  take rate {tr:6.1%}  avg wait {w:5.2f} min  earnings/engaged hr ${e:,.2f}")
    print(f"Warehouse: {db_path.relative_to(ROOT)}   Exports: {exports.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
