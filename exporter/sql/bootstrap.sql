-- Idempotent Postgres bootstrap for the analytics export pipeline.
-- Runs as the postgres superuser on every pipeline run; every statement must be re-runnable.
-- (docker-entrypoint-initdb.d is not usable here: the db_data volume is already initialized,
--  so init scripts would never execute.)

CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS analytics_stage;

-- per-statement call counts/timings for Grafana (library preloaded via compose command)
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- ---- published marts (what Hasura tracks) ----------------------------------

CREATE TABLE IF NOT EXISTS analytics.taxi_zones (
    zone_id      integer PRIMARY KEY,
    borough      text NOT NULL,
    zone_name    text NOT NULL,
    service_zone text
);

CREATE TABLE IF NOT EXISTS analytics.zone_daily_stats (
    zone_id         integer NOT NULL REFERENCES analytics.taxi_zones (zone_id),
    day             date    NOT NULL,
    trips           bigint  NOT NULL,
    total_revenue   numeric(14,2) NOT NULL,
    avg_distance_km numeric(8,3),
    avg_tip_pct     numeric(6,4),
    PRIMARY KEY (zone_id, day)
);
CREATE INDEX IF NOT EXISTS zone_daily_stats_day_idx
    ON analytics.zone_daily_stats (day);

CREATE TABLE IF NOT EXISTS analytics.payment_daily_stats (
    payment_type text   NOT NULL,
    day          date   NOT NULL,
    trips        bigint NOT NULL,
    total_amount numeric(14,2) NOT NULL,
    PRIMARY KEY (payment_type, day)
);

-- ---- ops ledger: one row per pipeline run -----------------------------------

CREATE TABLE IF NOT EXISTS analytics._export_runs (
    run_id      text PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL,
    rows_loaded jsonb,
    error       text
);

-- ---- load targets: same column order as the marts, no constraints -----------

CREATE TABLE IF NOT EXISTS analytics_stage.taxi_zones (
    zone_id integer, borough text, zone_name text, service_zone text
);
CREATE TABLE IF NOT EXISTS analytics_stage.zone_daily_stats (
    zone_id integer, day date, trips bigint, total_revenue numeric(14,2),
    avg_distance_km numeric(8,3), avg_tip_pct numeric(6,4)
);
CREATE TABLE IF NOT EXISTS analytics_stage.payment_daily_stats (
    payment_type text, day date, trips bigint, total_amount numeric(14,2)
);

-- ---- least privilege: exporter_role owns the analytics schemas only ---------
-- (ownership rather than plain grants: ANALYZE requires table ownership on PG 15)
-- exporter_role gets nothing on public.*; the role itself is created by export.py
-- because CREATE ROLE cannot take a parameterized password.

GRANT USAGE ON SCHEMA analytics, analytics_stage TO exporter_role;

ALTER TABLE analytics.taxi_zones          OWNER TO exporter_role;
ALTER TABLE analytics.zone_daily_stats    OWNER TO exporter_role;
ALTER TABLE analytics.payment_daily_stats OWNER TO exporter_role;
ALTER TABLE analytics._export_runs        OWNER TO exporter_role;

ALTER TABLE analytics_stage.taxi_zones          OWNER TO exporter_role;
ALTER TABLE analytics_stage.zone_daily_stats    OWNER TO exporter_role;
ALTER TABLE analytics_stage.payment_daily_stats OWNER TO exporter_role;

-- ---- observability reader: Grafana's datasource role -----------------------
-- pg_monitor grants read access to pg_stat_activity / pg_stat_statements;
-- SELECT on analytics.* covers the run ledger and mart dashboards. Nothing else.

GRANT pg_monitor TO grafana_reader;
GRANT USAGE ON SCHEMA analytics TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO grafana_reader;
