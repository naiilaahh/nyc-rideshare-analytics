-- Silver layer: one row per completed Uber / Lyft trip, typed, enriched and flagged.
-- Rows are FLAGGED, never silently dropped, so trip totals still match TLC's published counts.
-- Data-quality rules DQ-01..DQ-08 are documented in docs/data_quality_rules.md.
--
-- Placeholders filled by scripts/01_build_warehouse.py:
--   {source}      read_parquet(...) over the raw monthly files (or the sample CSV)
--   {cbd_fee}     cbd_congestion_fee, or 0 when no 2025 file is loaded
--   {start}/{end} reporting window (pickup date, end exclusive)
with base as (
    select
        case hvfhs_license_num when 'HV0003' then 'Uber' when 'HV0005' then 'Lyft' else 'Other' end as company,
        cast(request_datetime  as timestamp)  as request_ts,
        cast(on_scene_datetime as timestamp)  as on_scene_ts,
        cast(pickup_datetime   as timestamp)  as pickup_ts,
        cast(dropoff_datetime  as timestamp)  as dropoff_ts,
        cast(PULocationID as integer)         as pu_zone_id,
        cast(DOLocationID as integer)         as do_zone_id,
        cast(trip_miles   as double)          as trip_miles,
        cast(trip_time    as bigint)          as trip_seconds,
        cast(base_passenger_fare as double)   as base_passenger_fare,
        cast(tolls as double)                 as tolls,
        cast(bcf as double)                   as bcf,
        cast(sales_tax as double)             as sales_tax,
        cast(congestion_surcharge as double)  as congestion_surcharge,
        coalesce(try_cast(airport_fee as double), 0)  as airport_fee,
        coalesce(try_cast({cbd_fee} as double), 0)   as cbd_congestion_fee,
        cast(tips as double)                  as tips,
        cast(driver_pay as double)            as driver_pay,
        shared_request_flag = 'Y'             as is_shared_request,
        shared_match_flag   = 'Y'             as is_shared_match,
        wav_request_flag    = 'Y'             as is_wav_request,
        wav_match_flag      = 'Y'             as is_wav_match,
        access_a_ride_flag  = 'Y'             as is_access_a_ride
    from {source}
    -- DQ-07: attribute trips by PICKUP time; files occasionally hold trips from adjacent months
    where cast(pickup_datetime as date) >= date '{start}'
      and cast(pickup_datetime as date) <  date '{end}'
      and hvfhs_license_num in ('HV0003', 'HV0005')
)

select
    b.*,
    cast(b.pickup_ts as date)                                          as pickup_date,
    cast(date_trunc('month', b.pickup_ts) as date)                     as pickup_month,
    strftime(b.pickup_ts, '%Y-%m')                                     as pickup_ym,
    date_trunc('hour', b.pickup_ts)                                    as pickup_hour,
    isodow(b.pickup_ts)                                                as pickup_dow,          -- 1 = Monday
    hour(b.pickup_ts)                                                  as pickup_hour_of_day,
    cast(date_trunc('week', b.pickup_ts) as date)                      as pickup_week,         -- ISO week, Monday start
    b.base_passenger_fare + b.tolls + b.bcf + b.sales_tax + b.congestion_surcharge
        + b.airport_fee + b.cbd_congestion_fee + b.tips                as rider_total,
    date_diff('second', b.request_ts, b.pickup_ts)                     as wait_seconds,

    -- DQ-01 unknown / outside-NYC zones (264 Unknown, 265 Outside of NYC): kept in totals, excluded from zone views
    (b.pu_zone_id in (264, 265))                                       as is_unknown_pu_zone,
    -- DQ-02 wait is valid only when request <= pickup and the wait is under 2 hours
    (b.request_ts <= b.pickup_ts
        and date_diff('second', b.request_ts, b.pickup_ts) <= 7200)   as is_valid_wait,
    -- DQ-03 + DQ-04 economics: positive fare under $1,000, positive distance and duration, trip under 4 h / 150 mi
    (b.base_passenger_fare > 0 and b.base_passenger_fare <= 1000
        and b.trip_miles > 0 and b.trip_miles <= 150
        and b.trip_seconds > 0 and b.trip_seconds <= 14400
        and b.driver_pay >= 0)                                         as is_valid_economics,
    -- DQ-05 driver pay above fare + tips: usually incentives, so flagged for analysis, not excluded
    (b.driver_pay > b.base_passenger_fare + b.tips + 5)               as is_pay_above_fare,
    coalesce(pz.is_crz, false) or coalesce(dz.is_crz, false)           as touches_crz,
    coalesce(pz.is_crz, false)                                         as is_crz_pickup,
    coalesce(pz.is_airport, false) or coalesce(dz.is_airport, false)   as is_airport_trip
from base as b
left join zone_attributes as pz on pz.zone_id = b.pu_zone_id
left join zone_attributes as dz on dz.zone_id = b.do_zone_id
