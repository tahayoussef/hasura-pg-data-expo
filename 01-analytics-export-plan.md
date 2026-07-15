# Plan: Internet Data → DuckDB Warehouse → Postgres → Hasura (BigQuery stand-in: DuckDB)

**Status: Phases 0–2 implemented and verified (2026-07-13). Phase 3 (hardening) dropped for now.**
**Phase 2 details and demo queries: see `02-analytics-graphql.md`. Metadata lives in `hasura/metadata.json`.**
**Multi-app consumer simulation (JWT auth, per-app roles) + IaC provisioning map: see `03-multi-app-consumers.md`.**
**Observability (Grafana over Postgres, dashboard as code): see `04-observability.md` — <http://localhost:3000>.**

## Runbook

```sh
docker compose up -d                          # base stack (postgres, hasura, connector)
docker compose run --rm exporter              # full pipeline run (first run downloads ~48 MB)
docker compose run --rm exporter --inspect    # ~10s read-only look at warehouse contents
docker compose exec postgres psql -U postgres \
  -c 'SELECT * FROM analytics._export_runs ORDER BY started_at DESC LIMIT 5;'   # run history
```

- Hasura console now requires the admin secret from `.env` (default `myadminsecretkey`).
- Add months to `exporter/config.yml` to backfill; already-ingested files are cached in the warehouse volume and never re-downloaded.
- Reset the warehouse/staging entirely: `docker compose down && docker volume rm hasura-pg-data-expo_warehouse hasura-pg-data-expo_staging`.

## Goal

Simulate the production pattern *"an analytical warehouse ingests raw data, transforms it, and periodically exports curated marts into the operational Postgres that Hasura serves"*, entirely on this machine, with DuckDB standing in for BigQuery.

The data is **real data fetched from the internet** — not tied to the `authors`/`articles` demo tables. The pipeline creates its own, new Postgres resources.

```
 (prod)   external sources ─▶ BigQuery (raw ─▶ marts) ─▶ GCS Parquet ─▶ Cloud SQL ─▶ Hasura
 (local)  public dataset  ─▶ DuckDB   (raw ─▶ marts) ─▶ /staging Parquet ─▶ Postgres ─▶ Hasura
```

## Key design decision: DuckDB is embedded, not a server

There is no official DuckDB server container — DuckDB is a library/CLI that runs *inside* a process, like SQLite. So "containerized DuckDB" here means a **one-shot job container** (`exporter`) that:

1. downloads/reads the public dataset (DuckDB `httpfs` reads Parquet/CSV straight over HTTPS),
2. persists it into a DuckDB file on a volume (the "warehouse"),
3. transforms raw data into small curated **marts**,
4. stages the marts as Parquet on a shared volume (the "GCS bucket"),
5. bulk-loads them into Postgres via DuckDB's `postgres` extension.

This mirrors prod: BigQuery→Postgres pipelines export to GCS and bulk-load; they don't stream rows.

> Alternative considered: ClickHouse (a real analytical *server*) is a closer infrastructural analog to BigQuery, but DuckDB is far lighter, reads remote Parquet natively, and the *pipeline shape* (ingest → transform → stage → load) is what we're simulating. Sticking with DuckDB.

## Dataset: NYC TLC taxi trips (default, swappable)

**Recommended default: NYC Yellow Taxi trip records** — the canonical public analytical dataset.

- **Fact data**: monthly Parquet files, ~3M rows / ~50 MB each, hosted by the TLC:
  `https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet`
- **Dimension data**: taxi zone lookup CSV (265 zones, borough + zone name):
  `https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv`
- Realistic analytical shape: timestamps, locations, fares, payment types — perfect for rollups.
- Public, free, no API key, stable URLs, permissive terms.

**Swappability is a design requirement**: the ingest step is driven by a small config (source name, URLs/months, format), so replacing taxi data with GH Archive events, NOAA weather, OpenSky flights, etc. only changes config + transform SQL, not pipeline code.

### Warehouse layering (BigQuery-style)

| Layer | Where | Content |
|---|---|---|
| `raw` | DuckDB schema | trip Parquet as-ingested (millions of rows), zone lookup as-ingested |
| `marts` | DuckDB schema | small curated tables produced by transform SQL |
| exported | Postgres `analytics` schema | the marts only — **raw data never leaves the warehouse** (prod rule: you export curated marts, not raw events) |

### New Postgres resources created by the pipeline

All in a new `analytics` schema (plus `analytics_stage` for loading):

| Table | Grain | Example columns |
|---|---|---|
| `analytics.taxi_zones` | 1 row / zone (dim, ~265 rows) | `zone_id (pk)`, `borough`, `zone_name`, `service_zone` |
| `analytics.zone_daily_stats` | 1 row / pickup-zone / day (fact) | `zone_id (fk)`, `day`, `trips`, `total_revenue`, `avg_distance_km`, `avg_tip_pct` |
| `analytics.payment_daily_stats` | 1 row / payment-type / day | `payment_type`, `day`, `trips`, `total_amount` |
| `analytics._export_runs` | 1 row / pipeline run (ops ledger) | `run_id`, `started_at`, `finished_at`, `status`, `rows_loaded jsonb`, `error` |

Explicit DDL (no inferred types), primary keys, FK `zone_daily_stats.zone_id → taxi_zones.zone_id`, indexes on `(day)`, created **after** load.

### Hasura exposure this enables

- Track the three tables; FK gives suggested relationships: `taxi_zones.daily_stats` (array) and `zone_daily_stats.zone` (object) → nested queries like *"busiest Manhattan zones per day"*.
- An aggregate-friendly shape for GraphQL `_aggregate` queries.
- **Subscription demo**: subscribe to `_export_runs` (or `zone_daily_stats` for a given day) and watch a pipeline run land live.
- `analyst` role with select-only permissions on `analytics.*`.

## Architecture

```
                       internet (HTTPS)
                             │  1. ingest (httpfs, cached)
docker compose network       ▼
┌─────────────────────────────────────────────────────────────────┐
│  exporter (one-shot job container, python:slim + duckdb)        │
│  ├── /warehouse  volume: analytics.duckdb  (raw + marts)        │
│  ├── /staging    volume: Parquet per run   (the "GCS bucket")   │
│  │   2. transform raw → marts                                   │
│  │   3. COPY marts TO '/staging/<run_id>/*.parquet'             │
│  │   4. ATTACH postgres → bulk INSERT into analytics_stage.*    │
│  │   5. atomic publish → analytics.* + ledger row               │
│  ▼                                                              │
│  postgres (existing container)                                  │
│  ├── public.*          ← untouched (authors/articles demo)      │
│  ├── analytics.*       ← new: marts + _export_runs              │
│  └── analytics_stage.* ← load target before publish             │
│  ▲                                                              │
│  graphql-engine (existing) — tracks analytics.* read-only       │
└─────────────────────────────────────────────────────────────────┘
```

## Local-environment constraints (and how the plan handles them)

| Constraint | Impact | Handling |
|---|---|---|
| **Outbound internet from a container** | Ingest needs HTTPS egress; corporate proxy/VPN or Docker DNS issues would break it | First run performs a connectivity preflight with a clear error; downloads are **cached on the `/warehouse` volume** so subsequent runs work offline |
| Raw data is millions of rows | Don't shove it into Postgres | Only marts (≤ a few thousand rows) are exported; raw stays in DuckDB |
| Postgres port **not published to host** | Host tools can't reach the DB | Exporter runs *inside* the compose network (`postgres:5432`). Optionally publish `5433:5432` for psql/DBeaver debugging |
| `db_data` volume **already initialized** | `/docker-entrypoint-initdb.d` scripts will **not** run (they only execute on an empty data dir) | Bootstrap DDL (schemas, roles, tables, ledger) runs as an idempotent migration step inside the exporter (`CREATE SCHEMA IF NOT EXISTS …`) |
| Windows host (CRLF) | Shell scripts copied into Linux containers break with `\r` | Python entrypoint instead of .sh; `.gitattributes` with `*.sh text eol=lf` regardless |
| Docker Desktop bind-mount perf on Windows | Slow Parquet I/O on bind mounts | Named volumes for `/warehouse` and `/staging` |
| Docker Desktop memory ceiling | DuckDB defaults to grabbing most available RAM; a 3M-row aggregate is fine but unbounded ingest isn't | DuckDB `memory_limit` (e.g. `1GB`), compose `mem_limit`, ingest limited to N configured months |
| Hasura metadata lives in the **same** Postgres | ETL shares I/O with Hasura | Fine locally; flagged as prod concern #11 below |
| No healthcheck on the postgres service | Exporter may start before PG accepts connections | Add `pg_isready` healthcheck; exporter uses `depends_on: condition: service_healthy` + retry |
| No admin secret on Hasura | Anyone reaching :8080 is admin | Set `HASURA_GRAPHQL_ADMIN_SECRET` while touching the compose file |

## Production concerns: flagged, and what we do locally

| # | Concern | Local implementation | Production equivalent |
|---|---|---|---|
| 1 | **Ingestion reliability** (source down, throttling, partial downloads) | Retry with backoff; download to temp then atomic rename into cache; checksum/row-count sanity check before committing to `raw` | Managed transfers (BigQuery Data Transfer Service), dead-letter handling, alerting |
| 2 | **Source pinning & reproducibility** | Config pins exact dataset files (e.g. `2024-01`…`2024-03`); cache makes runs reproducible offline | Pinned snapshot tables / partition decorators; never "latest" in prod jobs |
| 3 | **Source schema drift** (TLC has changed columns across years — real!) | Ingest selects an **explicit column list** with casts; unknown columns ignored, missing ones fail loudly | Schema registry / contract tests on external sources |
| 4 | **Licensing/ToS of external data** | TLC data is public & permissively licensed; noted in config | Legal review, data-governance catalog |
| 5 | **Idempotency / retries** | Every run has a `run_id`; load lands in `analytics_stage`; publish is transactional; re-running a failed run is safe | Orchestrator retries with backoff; dedup on run_id |
| 6 | **Atomic publish** (readers never see partial data) | `BEGIN; TRUNCATE analytics.x; INSERT …; COMMIT;` — MVCC keeps old data visible until commit. ⚠️ `TRUNCATE` takes `ACCESS EXCLUSIVE`, so concurrent reads block during the txn — acceptable at mart scale (thousands of rows) | Blue/green rename swap, partition exchange, or `MERGE` to avoid long locks |
| 7 | **Incremental vs full refresh** | Phase 1: full refresh (marts are small). Phase 3: per-month partitioned ingest + `ON CONFLICT (zone_id, day) DO UPDATE` | Watermark/CDC, merge loads; full refresh only for small dims |
| 8 | **Observability** | `analytics._export_runs` ledger (run_id, timings, per-table row counts, status, error). Demo: Hasura subscription on it | Metrics + alerting, data-quality dashboards, SLAs |
| 9 | **Type mapping** | Explicit Postgres DDL; DuckDB `TIMESTAMP`→`timestamptz` (UTC), `DECIMAL`→`numeric(p,s)`, `DOUBLE` money fields cast to `numeric(12,2)` at transform time | Same discipline for BigQuery: `INT64`→`bigint`, `NUMERIC`→`numeric(38,9)`, `TIMESTAMP`→`timestamptz`, `STRUCT/ARRAY`→`jsonb` |
| 10 | **Least privilege** | PG role `exporter_role`: rights on `analytics*` schemas only, zero on `public`. Hasura `analyst` role: select-only | IAM service accounts per pipeline; Hasura row/column permissions |
| 11 | **Blast radius on the OLTP DB** | Same instance, separate schema — acceptable locally only | Separate instance/database; Hasura reads analytics from a replica; ETL never saturates the OLTP primary |
| 12 | **Secrets** | `.env` (gitignored) feeding compose; no credentials committed | Secret Manager / Vault; short-lived creds |
| 13 | **Orchestration** | One-shot `docker compose run --rm exporter`; optional loop `scheduler` service behind compose `--profile etl` | Airflow/Dagster/Composer or Cloud Scheduler + Cloud Run jobs; backfill support |
| 14 | **Data-quality gate before publish** | Refuse to publish if a mart is empty or row count drops >50% vs previous run (ledger comparison) | dbt tests / Great Expectations, quarantine tables |
| 15 | **Hasura metadata as code** | After console setup, `hasura metadata export` → commit `hasura/` dir | `hasura metadata apply` in CI/CD; console disabled in prod |
| 16 | **Staging retention** | Keep last 5 run dirs in `/staging`, prune older | GCS lifecycle rules |

## Implementation phases

### Phase 0 — Prep (compose + bootstrap)
1. `pg_isready` healthcheck on `postgres`.
2. `.env` + `.env.example` (+ `.gitignore`): `POSTGRES_PASSWORD`, `HASURA_GRAPHQL_ADMIN_SECRET`, exporter creds.
3. Named volumes `warehouse`, `staging`.
4. Optional: publish Postgres on `5433` for host debugging.

### Phase 1 — Exporter: ingest + transform + load (full refresh)
```
exporter/
  Dockerfile          # python:3.12-slim + duckdb
  export.py           # pipeline entrypoint, step-structured, run_id logging
  config.yml          # dataset source(s): name, URLs/months, format, column contract
  sql/
    bootstrap.sql     # idempotent PG DDL: schemas, roles, grants, marts, _export_runs
    transform.sql     # DuckDB: raw trips+zones → the three marts
```
Steps in `export.py` (each logged, ledger `finally` writes success/failure):
1. **bootstrap** Postgres (idempotent DDL).
2. **ingest** — for each configured month: if not cached in `/warehouse`, fetch via `httpfs` into `raw.trips`; fetch zone CSV into `raw.zones`; enforce column contract.
3. **transform** — build `marts.*` in DuckDB.
4. **stage** — `COPY marts TO '/staging/<run_id>/*.parquet'`.
5. **load** — `ATTACH` Postgres, insert Parquet into `analytics_stage.*`.
6. **publish** — one transaction: truncate + insert `analytics.*`, quality gate, `ANALYZE`, ledger row.

Run: `docker compose run --rm exporter`.

### Phase 2 — Hasura exposure
1. Track `analytics.taxi_zones`, `analytics.zone_daily_stats`, `analytics.payment_daily_stats`, `analytics._export_runs`.
2. Add FK-suggested relationships (`zone ↔ daily_stats`).
3. `analyst` role: select-only, column-limited; `_export_runs` restricted to an ops role.
4. Demo queries: nested zones→stats, `_aggregate` totals, subscription on `_export_runs` while a run executes.
5. `hasura metadata export` → commit `hasura/`.

### Phase 3 — Hardening (prod-parity extras)
- Incremental loads: month-partitioned ingest, `ON CONFLICT DO UPDATE` publish, `--backfill 2024-01` flag.
- Stricter data-quality gates (null ratios, value ranges).
- Staging pruning; `scheduler` loop service.
- Second data source behind the same config to prove swappability.

## Verification plan
1. Run exporter twice → both succeed (idempotent), ledger shows 2 runs, second reuses cached downloads (fast, offline-capable).
2. Kill exporter mid-load → `analytics.*` still serves the previous complete dataset; ledger row `failed`.
3. Disconnect network after first run → run still succeeds from cache (proves pinning/caching).
4. GraphQL: nested zone→stats query, aggregate query, live subscription during a run.
5. Role check: `analyst` can select `analytics.*` only; `exporter_role` cannot touch `public.*`.

## Out of scope (deliberately)
- Real CDC/streaming (Debezium, Datastream) — different pattern than batch export.
- Hasura remote sources / BigQuery connector — we simulate the *export* pattern, not query federation.
- Kubernetes/orchestrator deployment — compose is the simulation boundary.
