-- DuckDB transforms: raw -> marts. {min_day} / {end_day} are substituted by export.py
-- from the pinned months in config.yml.

CREATE SCHEMA IF NOT EXISTS marts;

CREATE OR REPLACE TABLE marts.taxi_zones AS
SELECT zone_id, borough, zone_name, service_zone
FROM raw.zones;

-- TLC files contain stray rows dated outside their own month (a real upstream
-- data-quality quirk) — keep only the pinned date range.
CREATE OR REPLACE TABLE marts.zone_daily_stats AS
SELECT
    t.pu_zone_id                       AS zone_id,
    CAST(t.pickup_at AS DATE)          AS day,
    COUNT(*)                           AS trips,
    CAST(SUM(t.total_amount) AS DECIMAL(14,2))                    AS total_revenue,
    CAST(AVG(t.trip_distance_miles) * 1.60934 AS DECIMAL(8,3))    AS avg_distance_km,
    -- fares under $1 and tip ratios above 10x are meter junk, so exclude/clamp them
    CAST(AVG(CASE WHEN t.fare_amount >= 1
                  THEN LEAST(t.tip_amount / t.fare_amount, 10) END) AS DECIMAL(6,4)) AS avg_tip_pct
FROM raw.trips t
JOIN raw.zones z ON z.zone_id = t.pu_zone_id
WHERE t.pickup_at >= TIMESTAMP '{min_day} 00:00:00'
  AND t.pickup_at <  TIMESTAMP '{end_day} 00:00:00'
GROUP BY 1, 2;

CREATE OR REPLACE TABLE marts.payment_daily_stats AS
SELECT
    CASE t.payment_type
        WHEN 1 THEN 'credit_card'
        WHEN 2 THEN 'cash'
        WHEN 3 THEN 'no_charge'
        WHEN 4 THEN 'dispute'
        WHEN 5 THEN 'unknown'
        WHEN 6 THEN 'voided'
        ELSE 'other'
    END                                AS payment_type,
    CAST(t.pickup_at AS DATE)          AS day,
    COUNT(*)                           AS trips,
    CAST(SUM(t.total_amount) AS DECIMAL(14,2)) AS total_amount
FROM raw.trips t
WHERE t.pickup_at >= TIMESTAMP '{min_day} 00:00:00'
  AND t.pickup_at <  TIMESTAMP '{end_day} 00:00:00'
GROUP BY 1, 2;
