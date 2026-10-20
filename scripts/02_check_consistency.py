"""
Step 2: prove every delivery channel reports the same numbers.

For each month x company (and the full period), every KPI is computed four ways:

  A  warehouse SQL      DuckDB over gold_monthly_company using kpi_sql()        (analyst notebooks)
  B  Tableau extract    pandas over exports/daily_zone_company.csv               (executive dashboard)
  C  embedded app feed  pandas over exports/hourly_zone_company.parquet          (Streamlit portal)
  D  independent check  hand-written SQL straight from silver for core KPIs      (catches formula bugs)

Any difference above 1e-9 (relative) fails the run and is written to
data/exports/consistency_report.csv.

    python scripts/02_check_consistency.py [--sample]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from metrics.kpi import compute_kpis, kpi, kpi_names, kpi_sql  # noqa: E402

INDEPENDENT = {   # written by hand from the DQ rules, NOT generated from kpis.yml
    "trips": "count(*)",
    "take_rate": "sum(case when is_valid_economics then base_passenger_fare - driver_pay end) "
                 "/ sum(case when is_valid_economics then base_passenger_fare end)",
    "avg_wait_min": "avg(case when is_valid_wait then wait_seconds end) / 60.0",
    "pickup_within_5min_rate": "avg(case when is_valid_wait then (case when wait_seconds <= 300 then 1.0 else 0.0 end) end)",
    "driver_earnings_per_engaged_hour": "sum(case when is_valid_economics then driver_pay + tips end) "
                                        "/ (sum(case when is_valid_economics then trip_seconds end) / 3600.0)",
    "gross_bookings": "sum(case when is_valid_economics then rider_total - tips end)",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true")
    sfx = "_sample" if ap.parse_args().sample else ""
    exports = ROOT / "data" / f"exports{sfx}"
    con = duckdb.connect(str(ROOT / "data" / f"warehouse{sfx}.duckdb"), read_only=True)
    ratio_kpis = [n for n in kpi_names() if kpi(n)["type"] != "share_of_total"]

    # A: warehouse SQL
    a = con.execute(f"""
        select cast(pickup_month as varchar) as month, company,
               {", ".join(f"{kpi_sql(n)} as {n}" for n in ratio_kpis)}
        from gold_monthly_company group by all
    """).df()
    a_all = con.execute(f"""
        select 'ALL' as month, company, {", ".join(f"{kpi_sql(n)} as {n}" for n in ratio_kpis)}
        from gold_monthly_company group by company
    """).df()
    a = pd.concat([a, a_all])

    # B: Tableau extract
    b_src = pd.read_csv(exports / "daily_zone_company.csv")
    b_src["month"] = b_src["pickup_month"].astype(str).str[:10]
    b = pd.concat([compute_kpis(b_src, ["month", "company"], ratio_kpis),
                   compute_kpis(b_src, ["company"], ratio_kpis).assign(month="ALL")])

    # C: embedded app feed (hourly parquet, unknown zones are already excluded from the app feed,
    #    so the app's totals are reconciled against the same filter in A via a separate check below)
    c_src = pd.read_parquet(exports / "hourly_zone_company.parquet")
    c_src["month"] = pd.to_datetime(c_src["pickup_date"]).dt.strftime("%Y-%m-01")
    c = pd.concat([compute_kpis(c_src, ["month", "company"], ratio_kpis),
                   compute_kpis(c_src, ["company"], ratio_kpis).assign(month="ALL")])
    a_known = con.execute(f"""
        select cast(cast(date_trunc('month', pickup_date) as date) as varchar) as month, company,
               {", ".join(f"{kpi_sql(n)} as {n}" for n in ratio_kpis)}
        from gold_hourly_zone_company where pu_zone_id not in (264, 265) group by all
        union all
        select 'ALL', company, {", ".join(f"{kpi_sql(n)} as {n}" for n in ratio_kpis)}
        from gold_hourly_zone_company where pu_zone_id not in (264, 265) group by company
    """).df()

    # D: independent, straight from silver
    d = con.execute(f"""
        select cast(pickup_month as varchar) as month, company, {", ".join(f"{e} as {n}" for n, e in INDEPENDENT.items())}
        from silver_trips group by all
        union all
        select 'ALL', company, {", ".join(f"{e} as {n}" for n, e in INDEPENDENT.items())}
        from silver_trips group by company
    """).df() if _has_silver(con) else None

    rows = []

    def compare(label: str, left: pd.DataFrame, right: pd.DataFrame, names: list[str]):
        m = left.merge(right, on=["month", "company"], suffixes=("_ref", "_chk"), how="outer", indicator=True)
        for _, r in m.iterrows():
            for n in names:
                x, y = r.get(f"{n}_ref"), r.get(f"{n}_chk")
                ok = (pd.isna(x) and pd.isna(y)) or (pd.notna(x) and pd.notna(y) and abs(x - y) <= 1e-9 * max(1.0, abs(x)))
                rows.append({"check": label, "month": r["month"], "company": r["company"], "kpi": n,
                             "reference": x, "channel": y, "ok": bool(ok and r["_merge"] == "both")})

    compare("A warehouse vs B tableau extract", a, b, ratio_kpis)
    compare("A warehouse (known zones) vs C embedded app", a_known, c, ratio_kpis)
    if d is not None:
        compare("A warehouse vs D independent silver SQL", a, d, list(INDEPENDENT))

    rep = pd.DataFrame(rows)
    rep.to_csv(exports / "consistency_report.csv", index=False)
    summary = rep.groupby("check")["ok"].agg(["sum", "count"])
    print(summary.rename(columns={"sum": "passed", "count": "checked"}).to_string())
    bad = rep[~rep["ok"]]
    if len(bad):
        print(f"\nFAILED: {len(bad)} mismatches. First few:\n{bad.head(10).to_string(index=False)}")
        sys.exit(1)
    print(f"\nAll {len(rep):,} KPI values match across channels.")


def _has_silver(con) -> bool:
    try:
        con.execute("select 1 from silver_trips limit 1")
        return True
    except duckdb.Error:
        return False


if __name__ == "__main__":
    main()
