"""
Step 0: download real NYC TLC High-Volume For-Hire Vehicle (HVFHV) trip records, i.e. every
Uber and Lyft trip in New York City, and profile them.

Run from the project folder in the VS Code terminal:

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python scripts/00_get_and_profile_data.py                               # default: Oct 2024 - Mar 2025
    python scripts/00_get_and_profile_data.py --start 2024-10 --end 2024-10 # one-month trial (~0.5 GB)

The default window is 6 months around the start of Manhattan congestion pricing
(5 Jan 2025): ~120M trips, ~3 GB on disk.

Source: NYC Taxi & Limousine Commission, https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page
Company is identified by hvfhs_license_num: HV0003 = Uber, HV0005 = Lyft
(HV0002 Juno and HV0004 Via no longer operate in NYC).

Outputs (all under data/):
  raw/fhvhv_tripdata_YYYY-MM.parquet, raw/taxi_zone_lookup.csv
  profile/schema_by_month.csv, monthly_by_company.csv, null_rates.csv,
          anomalies_by_month.csv, profile_summary.json
  sample/fhvhv_sample.csv   (0.05% Bernoulli sample for SQL development)
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW, PROFILE, SAMPLE = ROOT / "data/raw", ROOT / "data/profile", ROOT / "data/sample"
CDN = "https://d37ci6vzurychx.cloudfront.net"

# python.org installers on macOS ship without root certificates; use certifi's bundle when available.
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()
COMPANY = "case hvfhs_license_num when 'HV0003' then 'Uber' when 'HV0005' then 'Lyft' " \
          "when 'HV0002' then 'Juno' when 'HV0004' then 'Via' else 'Other' end"


def months(start: str, end: str) -> list[str]:
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def download(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  have   {dest.name}")
        return True
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        t0 = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "portfolio-analytics/1.0"})
        with urllib.request.urlopen(req, timeout=300, context=SSL_CONTEXT) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.rename(dest)
        print(f"  got    {dest.name}  {dest.stat().st_size / 1e6:,.0f} MB  {time.time() - t0:,.0f}s")
        return True
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        print(f"  skip   {dest.name}  (HTTP {e.code}: not published yet?)")
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-10")
    ap.add_argument("--end", default="2025-03")
    ap.add_argument("--sample-pct", type=float, default=0.05)
    args = ap.parse_args()
    for d in (RAW, PROFILE, SAMPLE):
        d.mkdir(parents=True, exist_ok=True)

    print("1/3 Downloading")
    download(f"{CDN}/misc/taxi_zone_lookup.csv", RAW / "taxi_zone_lookup.csv")
    got = [m for m in months(args.start, args.end)
           if download(f"{CDN}/trip-data/fhvhv_tripdata_{m}.parquet", RAW / f"fhvhv_tripdata_{m}.parquet")]
    if not got:
        sys.exit("No trip files downloaded. Check your internet connection.")

    con = duckdb.connect()
    con.execute("set preserve_insertion_order = false")
    print("\n2/3 Profiling (a few minutes per month)")

    # schema drift between monthly files (e.g. cbd_congestion_fee appears in Jan 2025)
    schema_rows = []
    for m in got:
        schema_rows += [(m, n, t) for n, t in con.execute(
            f"select column_name, column_type from (describe select * from read_parquet('{RAW}/fhvhv_tripdata_{m}.parquet'))").fetchall()]
    con.execute("create table schema_by_month(month varchar, column_name varchar, column_type varchar)")
    con.executemany("insert into schema_by_month values (?, ?, ?)", schema_rows)
    con.execute(f"copy schema_by_month to '{PROFILE}/schema_by_month.csv' (header)")

    con.execute(f"""
        create view trips as
        select *, regexp_extract(filename, 'fhvhv_tripdata_(\\d{{4}}-\\d{{2}})', 1) as file_month,
               {COMPANY} as company
        from read_parquet('{RAW}/fhvhv_tripdata_*.parquet', union_by_name = true, filename = true)
    """)
    cols = [r[0] for r in con.execute("select column_name from (describe trips)").fetchall()
            if r[0] not in ("filename", "file_month", "company")]
    cbd = "coalesce(cbd_congestion_fee, 0)" if "cbd_congestion_fee" in cols else "0"

    print("  monthly_by_company")
    con.execute(f"""
        copy (
          select file_month, company,
                 count(*)                                                       as trips,
                 round(sum(base_passenger_fare), 0)                             as base_fare_sum,
                 round(sum(driver_pay), 0)                                      as driver_pay_sum,
                 round(sum(tips), 0)                                            as tips_sum,
                 round(sum({cbd}), 0)                                           as cbd_fee_sum,
                 round(avg(trip_miles), 3)                                      as avg_miles,
                 round(avg(trip_time) / 60.0, 2)                                as avg_trip_minutes,
                 round(median(date_diff('second', request_datetime, pickup_datetime)) / 60.0, 2)
                                                                                as median_wait_minutes,
                 round(100.0 * avg(case when shared_request_flag = 'Y' then 1 else 0 end), 2) as pct_shared_requested,
                 round(100.0 * avg(case when wav_request_flag = 'Y' then 1 else 0 end), 3)    as pct_wav_requested
          from trips group by 1, 2 order by 1, 2
        ) to '{PROFILE}/monthly_by_company.csv' (header)
    """)

    print("  null_rates")
    null_expr = ",\n".join(f"round(100.0 * count(*) filter (where \"{c}\" is null) / count(*), 3) as \"{c}\"" for c in cols)
    con.execute(f"copy (select file_month, {null_expr} from trips group by 1 order by 1) to '{PROFILE}/null_rates.csv' (header)")

    print("  anomalies_by_month")
    con.execute(f"""
        copy (
          select
            file_month, company,
            count(*)                                                                                        as trips,
            count(*) filter (where strftime(pickup_datetime, '%Y-%m') <> file_month)                        as pickup_outside_file_month,
            count(*) filter (where dropoff_datetime <= pickup_datetime)                                     as nonpositive_duration,
            count(*) filter (where trip_time > 4 * 3600)                                                    as trip_over_4h,
            count(*) filter (where abs(trip_time - date_diff('second', pickup_datetime, dropoff_datetime)) > 120)
                                                                                                            as trip_time_mismatch_2min,
            count(*) filter (where trip_miles <= 0)                                                         as zero_or_negative_miles,
            count(*) filter (where trip_miles > 150)                                                        as miles_over_150,
            count(*) filter (where base_passenger_fare <= 0)                                                as nonpositive_fare,
            count(*) filter (where base_passenger_fare > 1000)                                              as fare_over_1000,
            count(*) filter (where driver_pay < 0)                                                          as negative_driver_pay,
            count(*) filter (where driver_pay > base_passenger_fare + tips + 5 and base_passenger_fare > 0) as driver_pay_exceeds_fare,
            count(*) filter (where request_datetime > pickup_datetime)                                      as request_after_pickup,
            count(*) filter (where date_diff('minute', request_datetime, pickup_datetime) > 120)            as wait_over_2h,
            count(*) filter (where on_scene_datetime is null)                                               as on_scene_missing,
            count(*) filter (where PULocationID in (264, 265) or DOLocationID in (264, 265))                as unknown_zone,
            count(*) filter (where originating_base_num is null)                                            as originating_base_missing
          from trips group by 1, 2 order by 1, 2
        ) to '{PROFILE}/anomalies_by_month.csv' (header)
    """)

    summary = {
        "source": "NYC TLC High Volume FHV trip records",
        "months_loaded": got,
        "total_trips": con.execute("select count(*) from trips").fetchone()[0],
        "columns": cols,
        "license_numbers_seen": [r[0] for r in con.execute("select distinct hvfhs_license_num from trips").fetchall()],
        "columns_not_in_every_month": [r[0] for r in con.execute(
            "select column_name from schema_by_month group by 1 having count(distinct month) < "
            "(select count(distinct month) from schema_by_month)").fetchall()],
        "type_changes": [list(r) for r in con.execute(
            "select column_name, string_agg(distinct column_type, ' | ') from schema_by_month "
            "group by 1 having count(distinct column_type) > 1").fetchall()],
    }
    (PROFILE / "profile_summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print("\n3/3 Sampling")
    con.execute(f"""
        copy (select * exclude (filename) from trips using sample {args.sample_pct}% (bernoulli, 42))
        to '{SAMPLE}/fhvhv_sample.csv' (header)
    """)
    n = con.execute(f"select count(*) from read_csv_auto('{SAMPLE}/fhvhv_sample.csv')").fetchone()[0]
    print(f"\nDone. {summary['total_trips']:,} trips across {len(got)} months profiled; sample = {n:,} rows.")
    print("Profiling outputs are in data/profile/ and the sample is in data/sample/.")


if __name__ == "__main__":
    main()
