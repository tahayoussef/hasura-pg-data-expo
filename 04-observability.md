# Observability — Grafana over Postgres

> Grafana: <http://localhost:3000> — login `admin` / `grafana` (from `.env`).
> Dashboard: **Hasura → Hasura Observability** (auto-provisioned, refreshes every 10 s).

## Why Grafana-only (and not Prometheus)

Hasura's native Prometheus endpoint (`/v1/metrics`) is an **Enterprise-only** feature —
the OSS image in this stack doesn't expose it. A Prometheus + postgres-exporter stack
would therefore only ever see the database anyway, at the cost of two extra containers.

So the simplest faithful setup is **one Grafana container reading Postgres directly**,
which observes Hasura from the database side:

- **`pg_stat_statements`** — every SQL statement Hasura compiles from GraphQL, with
  call counts and timings. Run `python consumers/simulate.py` and watch the
  `json_agg(...)`/`json_build_object(...)` statements' call counts climb.
- **`pg_stat_activity`** — live connections and currently-executing queries.
- **`analytics._export_runs`** — pipeline health: status, duration, rows per run.
- **the marts themselves** — trips/revenue per day, by borough.

Production paths, in order of fidelity: Hasura EE / Hasura Cloud (native Prometheus
metrics: request rate, latency, subscription counts, per-operation stats) → a log
pipeline (Hasura's structured `http-log` JSON into Loki/ELK) → this DB-side approach.

## What was added

| Piece | Where |
|---|---|
| Grafana service + volume | `docker-compose.yml` (port 3000) |
| `pg_stat_statements` preload | `postgres` service `command` |
| Extension + `grafana_reader` role (`pg_monitor` + SELECT on `analytics.*` only, denied on `public.*`) | `exporter/sql/bootstrap.sql`, role created in `export.py` |
| Datasource as code | `grafana/provisioning/datasources/postgres.yml` |
| Dashboard as code | `grafana/provisioning/dashboards/hasura-observability.json` |

Everything is provisioned — a fresh `docker compose up -d` + one exporter run recreates
the whole observability setup with zero UI clicks. (IaC mapping: the provisioning
folder is what Terraform's Grafana provider or config-management would own in prod;
the reader role/grants belong to the migration pipeline.)

## Dashboard contents

- **Stats row**: last export run status (green/red/yellow), runs in 24 h, live DB
  connection count, tracked statement count.
- **Recent export runs**: the ledger with durations and row counts — failures show
  their error text.
- **Connections by application/state**: who is connected right now (Hasura's pool,
  Grafana, exporter when running).
- **Top statements by calls**: Hasura's compiled SQL ranked by frequency, with mean
  latency — the closest OSS gets to per-operation metrics.
- **Business panels**: trips/day and revenue/day-by-borough from the marts
  (dashboard's time window is pinned to Jan 2024 to match the pinned dataset).

## Demo flow

1. Open the dashboard.
2. `python consumers/simulate.py` a few times → statement call counts climb; the
   apps' queries appear in "Top statements".
3. `docker compose run --rm exporter` → a `running` row appears in the ledger panel,
   flips to `success`; connection count briefly rises.

## Reset statement stats

```sh
docker compose exec postgres psql -U postgres -c "SELECT pg_stat_statements_reset();"
```
