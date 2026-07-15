"""NYC taxi -> DuckDB warehouse -> Parquet staging -> Postgres exporter.

Pipeline steps (see 01-analytics-export-plan.md):
    bootstrap -> ingest -> transform -> stage -> load -> publish

Usage:
    python export.py            # full pipeline run
    python export.py --inspect  # quick look at warehouse contents, no pipeline
"""

import argparse
import logging
import shutil
import sys
import time
import uuid
from datetime import date
from os import environ
from pathlib import Path

import duckdb
import psycopg
import yaml
from psycopg import sql
from psycopg.types.json import Json

log = logging.getLogger("exporter")

DUCKDB_PATH = environ.get("DUCKDB_PATH", "/warehouse/analytics.duckdb")
STAGING_DIR = environ.get("STAGING_DIR", "/staging")
PG_ADMIN_URL = environ.get("PG_ADMIN_URL", "")
PG_EXPORTER_URL = environ.get("PG_EXPORTER_URL", "")
EXPORTER_PASSWORD = environ.get("EXPORTER_PASSWORD", "")
GRAFANA_READER_PASSWORD = environ.get("GRAFANA_READER_PASSWORD", "")
MEMORY_LIMIT = environ.get("DUCKDB_MEMORY_LIMIT", "1GB")
KEEP_STAGING_RUNS = 5

# publish order matters: dims before facts (FK on zone_daily_stats.zone_id)
MARTS = ["taxi_zones", "zone_daily_stats", "payment_daily_stats"]

# Explicit column contract for the source parquet: if the TLC renames or retypes
# a column (it has, across years), this SELECT fails loudly instead of silently
# ingesting a different shape.
TRIPS_SELECT = """
SELECT
    CAST(VendorID              AS INTEGER)   AS vendor_id,
    CAST(tpep_pickup_datetime  AS TIMESTAMP) AS pickup_at,
    CAST(tpep_dropoff_datetime AS TIMESTAMP) AS dropoff_at,
    CAST(passenger_count       AS INTEGER)   AS passenger_count,
    CAST(trip_distance         AS DOUBLE)    AS trip_distance_miles,
    CAST(PULocationID          AS INTEGER)   AS pu_zone_id,
    CAST(DOLocationID          AS INTEGER)   AS do_zone_id,
    CAST(payment_type          AS INTEGER)   AS payment_type,
    CAST(fare_amount           AS DOUBLE)    AS fare_amount,
    CAST(tip_amount            AS DOUBLE)    AS tip_amount,
    CAST(total_amount          AS DOUBLE)    AS total_amount,
    '{fname}'                                AS source_file
FROM read_parquet('{url}')
"""

TRIPS_DDL = """
CREATE TABLE IF NOT EXISTS raw.trips (
    vendor_id INTEGER, pickup_at TIMESTAMP, dropoff_at TIMESTAMP,
    passenger_count INTEGER, trip_distance_miles DOUBLE,
    pu_zone_id INTEGER, do_zone_id INTEGER, payment_type INTEGER,
    fare_amount DOUBLE, tip_amount DOUBLE, total_amount DOUBLE,
    source_file TEXT
)
"""


def read_config():
    with open(Path(__file__).parent / "config.yml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not cfg["source"]["months"]:
        raise RuntimeError("config.yml: source.months must list at least one month")
    return cfg


def month_bounds(months):
    """[min_day, end_day) date range covering the pinned months."""
    starts = []
    for m in months:
        y, mo = (int(p) for p in m.split("-"))
        starts.append(date(y, mo, 1))
    last = max(starts)
    end = date(last.year + 1, 1, 1) if last.month == 12 else date(last.year, last.month + 1, 1)
    return min(starts), end


def connect_duckdb(read_only=False):
    con = duckdb.connect(DUCKDB_PATH, read_only=read_only)
    con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
    return con


# ---- steps -------------------------------------------------------------------


def bootstrap_postgres():
    """Idempotent: role (password from env, can't live in bootstrap.sql), schemas, tables, grants."""
    ddl = (Path(__file__).parent / "sql" / "bootstrap.sql").read_text(encoding="utf-8")
    roles = [("exporter_role", EXPORTER_PASSWORD), ("grafana_reader", GRAFANA_READER_PASSWORD)]
    with psycopg.connect(PG_ADMIN_URL) as conn:
        for name, password in roles:
            conn.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{name}') THEN
                        CREATE ROLE {name} LOGIN;
                    END IF;
                END $$;
            """)
            conn.execute(
                sql.SQL("ALTER ROLE {role} WITH LOGIN PASSWORD {pw}").format(
                    role=sql.Identifier(name), pw=sql.Literal(password)
                )
            )
        conn.execute(ddl)
    log.info("bootstrap: postgres schemas/tables/role ready")


def fetch_with_retry(con, statement, url, attempts=3):
    for i in range(1, attempts + 1):
        try:
            con.execute(statement)
            return
        except duckdb.Error as e:
            if i == attempts:
                raise RuntimeError(
                    f"failed to fetch {url} after {attempts} attempts — "
                    f"check outbound internet access from Docker ({e})"
                ) from e
            log.warning("fetch attempt %d/%d for %s failed (%s), retrying", i, attempts, url, e)
            time.sleep(5 * i)


def ingest(con, cfg):
    """Fetch pinned source files not already in the warehouse. Cached files make re-runs offline-capable."""
    src = cfg["source"]
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute("""
        CREATE TABLE IF NOT EXISTS raw._ingest_log (
            source_url TEXT PRIMARY KEY,
            ingested_at TIMESTAMP DEFAULT current_timestamp,
            row_count BIGINT
        )
    """)
    con.execute(TRIPS_DDL)
    con.execute("LOAD httpfs")

    if not con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE schema_name = 'raw' AND table_name = 'zones'"
    ).fetchone()[0]:
        url = src["zones_url"]
        fetch_with_retry(
            con,
            f"""CREATE TABLE raw.zones AS
                SELECT CAST("LocationID" AS INTEGER) AS zone_id,
                       "Borough" AS borough, "Zone" AS zone_name, service_zone
                FROM read_csv('{url}', header = true)""",
            url,
        )
        n = con.execute("SELECT count(*) FROM raw.zones").fetchone()[0]
        con.execute("INSERT INTO raw._ingest_log (source_url, row_count) VALUES (?, ?)", [url, n])
        log.info("ingest: zones lookup fetched (%d zones)", n)

    for month in src["months"]:
        url = src["trips_url_template"].format(month=month)
        if con.execute("SELECT 1 FROM raw._ingest_log WHERE source_url = ?", [url]).fetchone():
            log.info("ingest: %s already in warehouse, skipping (cached)", month)
            continue
        fname = url.rsplit("/", 1)[-1]
        before = con.execute("SELECT count(*) FROM raw.trips").fetchone()[0]
        fetch_with_retry(con, "INSERT INTO raw.trips " + TRIPS_SELECT.format(fname=fname, url=url), url)
        added = con.execute("SELECT count(*) FROM raw.trips").fetchone()[0] - before
        if added < 1000:
            raise RuntimeError(f"ingest sanity check: only {added} rows from {url}")
        con.execute("INSERT INTO raw._ingest_log (source_url, row_count) VALUES (?, ?)", [url, added])
        log.info("ingest: %s fetched (%s rows)", month, f"{added:,}")


def transform(con, cfg):
    min_day, end_day = month_bounds(cfg["source"]["months"])
    script = (Path(__file__).parent / "sql" / "transform.sql").read_text(encoding="utf-8")
    script = script.format(min_day=min_day, end_day=end_day)
    # strip comment lines before splitting so a ';' inside a comment can't cut a statement
    sql_only = "\n".join(l for l in script.splitlines() if not l.strip().startswith("--"))
    for stmt in sql_only.split(";"):
        if stmt.strip():
            con.execute(stmt)
    counts = {t: con.execute(f"SELECT count(*) FROM marts.{t}").fetchone()[0] for t in MARTS}
    log.info("transform: marts built %s", counts)
    return counts


def stage(con, run_id):
    run_dir = Path(STAGING_DIR) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    for t in MARTS:
        con.execute(f"COPY marts.{t} TO '{run_dir / t}.parquet' (FORMAT PARQUET)")
    log.info("stage: parquet written to %s", run_dir)

    # retention: keep the newest KEEP_STAGING_RUNS run dirs (prod analog: GCS lifecycle rules)
    runs = sorted((d for d in Path(STAGING_DIR).iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    for old in runs[KEEP_STAGING_RUNS:]:
        shutil.rmtree(old, ignore_errors=True)
    return run_dir


def load_stage(con, run_dir):
    """Bulk-load staged parquet into analytics_stage.* as exporter_role."""
    with psycopg.connect(PG_EXPORTER_URL) as conn:
        conn.execute("TRUNCATE " + ", ".join(f"analytics_stage.{t}" for t in MARTS))
    con.execute("LOAD postgres")
    con.execute(f"ATTACH '{PG_EXPORTER_URL}' AS pg (TYPE postgres)")
    for t in MARTS:
        con.execute(f"INSERT INTO pg.analytics_stage.{t} SELECT * FROM read_parquet('{run_dir / t}.parquet')")
    log.info("load: analytics_stage populated")


def publish():
    """Atomic swap: readers keep seeing the previous dataset until COMMIT."""
    with psycopg.connect(PG_EXPORTER_URL) as conn:
        for t in MARTS:
            new = conn.execute(f"SELECT count(*) FROM analytics_stage.{t}").fetchone()[0]
            old = conn.execute(f"SELECT count(*) FROM analytics.{t}").fetchone()[0]
            if new == 0:
                raise RuntimeError(f"quality gate: analytics_stage.{t} is empty, refusing to publish")
            if old > 0 and new < old * 0.5:
                raise RuntimeError(
                    f"quality gate: analytics_stage.{t} has {new} rows vs {old} published "
                    f"(>50% drop), refusing to publish"
                )
        conn.execute("TRUNCATE " + ", ".join(f"analytics.{t}" for t in MARTS))
        for t in MARTS:  # dims before facts: FK on zone_daily_stats
            conn.execute(f"INSERT INTO analytics.{t} SELECT * FROM analytics_stage.{t}")
    with psycopg.connect(PG_EXPORTER_URL, autocommit=True) as conn:
        for t in MARTS:
            conn.execute(f"ANALYZE analytics.{t}")
    log.info("publish: analytics.* swapped atomically")


# ---- run ledger ----------------------------------------------------------------


def start_run(run_id):
    with psycopg.connect(PG_EXPORTER_URL) as conn:
        conn.execute(
            "INSERT INTO analytics._export_runs (run_id, status) VALUES (%s, 'running')", (run_id,)
        )


def finish_run(run_id, status, rows=None, error=None):
    with psycopg.connect(PG_EXPORTER_URL) as conn:
        conn.execute(
            """UPDATE analytics._export_runs
               SET finished_at = now(), status = %s, rows_loaded = %s, error = %s
               WHERE run_id = %s""",
            (status, Json(rows) if rows else None, error, run_id),
        )


# ---- entrypoints ----------------------------------------------------------------


def run_pipeline():
    cfg = read_config()
    run_id = uuid.uuid4().hex[:8]
    log.info("run %s starting (source: %s, months: %s)",
             run_id, cfg["source"]["name"], cfg["source"]["months"])
    bootstrap_postgres()
    start_run(run_id)
    try:
        con = connect_duckdb()
        ingest(con, cfg)
        counts = transform(con, cfg)
        run_dir = stage(con, run_id)
        load_stage(con, run_dir)
        publish()
        finish_run(run_id, "success", rows=counts)
        log.info("run %s succeeded: %s", run_id, counts)
    except Exception as e:
        log.exception("run %s failed", run_id)
        try:
            finish_run(run_id, "failed", error=str(e))
        except Exception:
            log.exception("run %s: could not record failure in ledger", run_id)
        sys.exit(1)


def inspect():
    """~10s read-only look at what the warehouse holds. No pipeline, no writes."""
    path = Path(DUCKDB_PATH)
    if not path.exists():
        print(f"warehouse not found at {DUCKDB_PATH} — run the pipeline first:")
        print("  docker compose run --rm exporter")
        return
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    print(f"warehouse: {DUCKDB_PATH} ({path.stat().st_size / 1024**2:.1f} MB)\n")
    tables = con.execute("""
        SELECT schema_name, table_name FROM duckdb_tables()
        WHERE schema_name IN ('raw', 'marts') AND table_name <> '_ingest_log'
        ORDER BY schema_name DESC, table_name
    """).fetchall()
    if not tables:
        print("no datasets ingested yet")
        return
    for schema, table in tables:
        n = con.execute(f'SELECT count(*) FROM "{schema}"."{table}"').fetchone()[0]
        print(f"  {schema + '.' + table:<32} {n:>12,} rows")
    print("\ningested source files:")
    for ts, url, n in con.execute(
        "SELECT ingested_at, source_url, row_count FROM raw._ingest_log ORDER BY ingested_at"
    ).fetchall():
        print(f"  {ts:%Y-%m-%d %H:%M}  {url.rsplit('/', 1)[-1]:<40} {n:>12,} rows")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true",
                        help="print warehouse contents and exit (read-only)")
    args = parser.parse_args()
    inspect() if args.inspect else run_pipeline()
