# PRD: NYC Market Analytics Suite

| | |
|---|---|
| **Product owner** | Nailah Franklin (Analytics Product Owner, NYC Market) |
| **Status** | MVP built: Phases 0–3 complete on public data |
| **Data** | NYC TLC High-Volume FHV trip records, Oct 2024 – Mar 2025 (121.4M Uber and Lyft trips) |
| **Stakeholders** | GM NYC, Finance, Marketplace Ops, Driver Experience, Policy & Strategy, Data Science |

> Independent portfolio project. The scenario casts a rideshare company's NYC market team as the
> customer; all data is public TLC data, and the project is not affiliated with Uber, Lyft or TLC.

## 1. Problem

The NYC market team answers the same questions in three different places, and gets three
different answers:

- **Executives** see a weekly deck built by hand from ad-hoc queries. "Take rate" means a
  different thing on each slide.
- **Analysts** start every question from raw trip files, re-deriving the same cleaning rules
  (and making different mistakes) each time.
- **Ops managers** find out about demand surges after the fact, from rider complaints.

There's also an urgent, specific question. **Congestion pricing started on 5 January 2025.**
Leadership needs to know what it did to trips, price, wait time and driver earnings in
Manhattan, and they need a number they can defend to regulators.

## 2. Goals and non-goals

**Goals**

1. One governed set of KPIs, defined once and identical in every channel.
2. A curated, documented data layer that analysts trust enough to stop using raw files.
3. An operational view that flags unusual zone demand within the hour it happens.
4. A defensible estimate of the congestion-pricing impact.

**Non-goals (v1)**

- Forecasting and pricing optimisation. Phase 4 candidates.
- Unfulfilled demand. TLC data contains completed trips only.
- True streaming. TLC publishes monthly, so v1 replays history (see §8).

## 3. Personas

| | Executive | Analyst / Data Scientist | Embedded ops user |
|---|---|---|---|
| **Who** | GM NYC, Finance lead | Marketplace & pricing analysts | Driver-ops / marketplace managers |
| **Job to be done** | "Tell me in 2 minutes how the market moved this month, and why." | "Give me clean, trusted tables so I can answer a new question today." | "Tell me where drivers are needed right now, and let me act." |
| **Cadence** | Weekly / monthly | Daily, ad hoc | Every hour of a shift |
| **Channel** | Executive dashboard notebook (Tableau / Power BI extracts optional) | Gold tables in DuckDB / Databricks + analysis notebooks | Driver Ops Portal (Streamlit), embedded per company |
| **Pain today** | Conflicting numbers, slow decks | Re-cleaning raw data, no definitions | Reactive, no baseline for "normal" |

## 4. Requirements

| ID | Requirement | Persona | Priority | Delivered by |
|---|---|---|---|---|
| R1 | Every KPI defined once, with owner and definition | All | Must | `metrics/kpis.yml`, `metrics/kpi.py` |
| R2 | Same KPI value in every channel, proven automatically | All | Must | `scripts/02_check_consistency.py` (532/532 checks pass) |
| R3 | Monthly scorecard: trips, share, bookings, margin, take rate, service, earnings | Exec | Must | `notebooks/00_executive_dashboard.ipynb` |
| R4 | Margin bridge: trips vs fare vs take-rate effect | Exec | Should | `notebooks/02_kpi_framework.ipynb` |
| R5 | Congestion pricing impact with method and caveats | Exec, Policy | Must | `notebooks/03_congestion_pricing_impact.ipynb` |
| R6 | Curated silver layer with documented DQ flags | Analyst | Must | `sql/silver_trips.sql`, `docs/data_quality_rules.md` |
| R7 | Gold tables at hour / day / week / month grain | Analyst | Must | `scripts/01_build_warehouse.py` |
| R8 | Spark version of the pipeline for scale | Analyst | Should | `notebooks/05_databricks_pyspark.ipynb` |
| R9 | Zone-hour anomaly alerts against a 4-week baseline | Ops | Must | `gold_zone_hour_scored` |
| R10 | Tenant isolation: each company sees only its own data | Ops | Must | `tenant_scope()` in the portal |
| R11 | Alerts can be acted on and the action is logged | Ops | Must | Portal action buttons → `action_log.csv` |
| R12 | Alert thresholds adjustable by the user | Ops | Should | Portal sidebar; tuning analysis in notebook 04 |

### Prioritisation (RICE)

| Feature | Reach | Impact | Confidence | Effort (wks) | Score | Phase |
|---|---|---|---|---|---|---|
| Governed KPI engine + consistency check | 80 users (all channels) | 3 | 90% | 2 | 108 | 1 |
| Executive scorecard | 15 execs/wk | 2 | 90% | 1 | 27 | 1 |
| Congestion pricing readout | 15 execs + policy | 3 | 70% | 1.5 | 21 | 1 |
| Silver/gold layer | 25 analysts | 2 | 90% | 2 | 22.5 | 1 |
| Zone anomaly alerts + portal | 40 ops users/day | 2 | 60% | 3 | 16 | 2–3 |
| Spark/Databricks port | 25 analysts | 1 | 80% | 1.5 | 13 | 2 |
| Demand forecasting | 40 ops users | 3 | 40% | 6 | 8 | 4 |

Reach = people who use the feature in a typical week. The KPI engine scores highest because
every channel depends on it, so it ships first.

## 5. Success metrics

| Category | Metric | Target |
|---|---|---|
| **Trust** | Cross-channel KPI mismatches | 0 (enforced by the consistency check) |
| **Trust** | Share of trips with a DQ flag explained in the rules doc | 100% |
| **Adoption** | Weekly active viewers of the exec dashboard / exec team size | ≥ 80% |
| **Adoption** | Analyst queries hitting gold tables vs raw files | ≥ 70% by end of Phase 2 |
| **Performance** | Full rebuild of 121M trips | < 5 min on a laptop (measured: about 1 min) |
| **Performance** | Portal page load / hour change | < 3 s |
| **Ops value** | Share of alerts actioned (from the action log) | ≥ 50%. Lower means thresholds are too loose. |
| **Ops value** | Alerts per manager per shift | 10–20 (alert-fatigue guardrail, see notebook 04) |
| **Outcome** | Change in pickup-within-5-min rate in alerted zones vs control zones | Positive (Phase 4 experiment) |

## 6. Key findings from discovery (Phase 0)

The full write-up is in `notebooks/01_data_discovery.ipynb`. These shaped the product:

1. **Lyft didn't report driver-arrival time** on 99.7% of trips until March 2025, so service
   KPIs use request-to-pickup time, which both companies report.
2. **Daylight saving time** makes timestamp-based durations wrong by an hour on 3 Nov and 9 Mar,
   so durations come from TLC's `trip_time` field.
3. **Lyft zero-fare trips spike** in December, January and March. They count as trips but are
   excluded from price and margin KPIs.
4. **The schema changes mid-period:** `cbd_congestion_fee` appears in January 2025.
5. **Driver pay exceeds the fare** on 2–5% of Uber trips, probably incentives. These trips are
   kept and flagged, because dropping them would overstate take rate.

Headline market facts (Oct 2024 → Mar 2025):

- **Market share:** Lyft rose from 24.4% to **29.2%**.
- **Take rate:** Uber's went from 23.4% to **28.1%**, while Lyft's stayed around 21–22%.
- **Pickups within 5 minutes:** 59–72% for Uber against 54–66% for Lyft.

## 7. Roadmap

| Phase | Scope | Exit criterion | Status |
|---|---|---|---|
| 0 · Discovery | Profile 121M trips, document data issues | DQ rules agreed with stakeholders | ✅ |
| 1 · Foundation | KPI engine, silver/gold layer, exec dashboard, congestion-pricing readout | Exec team uses the dashboard in the monthly review | ✅ |
| 2 · Analyst scale | Analysis notebooks, Databricks/PySpark port, Tableau/Power BI extracts | Spark and DuckDB gold match; analysts move off raw files | ✅ |
| 3 · Ops pilot | Driver Ops Portal with alerts and action logging, one tenant | 2-week pilot; ≥ 50% of alerts actioned | ✅ built, pilot pending |
| 4 · Intelligence | Demand forecasting, unmet-demand data (internal requests), A/B test of nudges | Measurable lift in the 5-minute pickup rate | Planned |
| 5 · Real time | Streaming ingest (Kafka → Delta), sub-minute alerts | Alert latency < 5 min | Planned |

## 8. Risks and assumptions

| Risk | Mitigation |
|---|---|
| "Real-time" is simulated: TLC data lags by about 2 months | Stated clearly in the portal. The architecture (gold hourly table + scoring) carries over to streaming in Phase 5. |
| Completed trips only, with no unmet demand | Treat alerts as *served-demand* anomalies. An internal deployment would score requests. |
| The congestion-pricing estimate is observational | Difference-in-differences against outer-borough trips, with caveats stated in the readout |
| Through-trips pay the CRZ fee without starting or ending in the zone | Measured: 53–73% of outbound trips from zones just north of 60th St are charged. Documented as control contamination. |
| KPI drift if a channel re-implements a formula | KPIs are only computed through `metrics/kpi.py`. The consistency check fails the build on any mismatch. |
