# Analytics over GraphQL — Phase 2 of the export pipeline

> Prereq: pipeline has run at least once (`docker compose run --rm exporter`), so
> `analytics.*` in Postgres holds the NYC taxi marts. See `01-analytics-export-plan.md`.
> Console now needs the admin secret from `.env` (default `myadminsecretkey`).

## What is exposed

| Table | GraphQL root field | Roles |
|---|---|---|
| `analytics.taxi_zones` | `analytics_taxi_zones` | admin, **analyst** |
| `analytics.zone_daily_stats` | `analytics_zone_daily_stats` | admin, **analyst** |
| `analytics.payment_daily_stats` | `analytics_payment_daily_stats` | admin, **analyst** |
| `analytics._export_runs` | `analytics__export_runs` | admin, **ops** only |
| `analytics_stage.*` | — deliberately untracked | nobody |

Design notes:

- **`analytics_stage.*` is not tracked.** Those tables are the pipeline's internal load
  targets; mid-run they hold partial data. Exposing them would leak exactly what the
  atomic publish protects against.
- **`analyst` is select-only** with explicit column lists and aggregations enabled —
  the GraphQL-level twin of the Postgres-level least privilege (`exporter_role` owns
  `analytics.*`, nothing on `public`).
- **`_export_runs` is ops-only**: pipeline observability is not analyst business data.
- Relationships (created from the FK): `taxi_zones.zone_daily_stats` (array) and
  `zone_daily_stats.taxi_zone` (object).

## Trying roles in GraphiQL

The console runs as `admin`. To act as another role, add a request header in GraphiQL:

```
X-Hasura-Role: analyst
```

## Demo queries

**Nested: busiest Manhattan zones, day by day** (role: analyst)

```graphql
query {
  analytics_taxi_zones(where: { borough: { _eq: "Manhattan" } }, limit: 3) {
    zone_name
    zone_daily_stats(order_by: { trips: desc }, limit: 3) {
      day
      trips
      total_revenue
    }
  }
}
```

**Aggregates: whole-month totals** (role: analyst)

```graphql
query {
  analytics_zone_daily_stats_aggregate {
    aggregate {
      sum { trips total_revenue }
      avg { avg_distance_km }
    }
  }
}
```

**Other direction, with nested filter: high-revenue days and where they happened** (role: analyst)

```graphql
query {
  analytics_zone_daily_stats(
    where: { total_revenue: { _gt: 100000 } }
    order_by: { total_revenue: desc }
    limit: 5
  ) {
    day
    trips
    total_revenue
    taxi_zone { zone_name borough }
  }
}
```

**Pipeline observability** (role: ops — analysts get `field not found`)

```graphql
query {
  analytics__export_runs(order_by: { started_at: desc }, limit: 5) {
    run_id
    status
    started_at
    finished_at
    rows_loaded
    error
  }
}
```

## Live subscription demo: watch a pipeline run land

1. In one GraphiQL tab (header `X-Hasura-Role: ops`), start:

```graphql
subscription {
  analytics__export_runs(order_by: { started_at: desc }, limit: 5) {
    run_id
    status
    rows_loaded
  }
}
```

2. In a terminal, kick off a run:

```sh
docker compose run --rm exporter
```

3. Watch the subscription push a `running` row, then flip to `success` with the row
   counts — the same live-update mechanic as `00-basic-hasura.md`, now driven by a
   real ETL job instead of a hand-written mutation.

The marts themselves also work with subscriptions (e.g. subscribe to
`analytics_zone_daily_stats` filtered to one zone) — the resultset updates the moment
a publish transaction commits, never showing a half-loaded state.

## Metadata as code

All of the above lives in `hasura/metadata.json` (exported via the metadata API — no
console-click drift). To restore it onto a fresh Hasura:

```sh
curl -s -X POST http://localhost:8080/v1/metadata \
  -H "Content-Type: application/json" \
  -H "X-Hasura-Admin-Secret: $HASURA_GRAPHQL_ADMIN_SECRET" \
  -d "{\"type\":\"replace_metadata\",\"args\":$(cat hasura/metadata.json)}"
```

After changing anything in the console, re-export so git stays the source of truth:

```sh
curl -s -X POST http://localhost:8080/v1/metadata \
  -H "Content-Type: application/json" \
  -H "X-Hasura-Admin-Secret: $HASURA_GRAPHQL_ADMIN_SECRET" \
  -d '{"type":"export_metadata","args":{}}' | python -m json.tool > hasura/metadata.json
```

(Production equivalent: the `hasura` CLI with a metadata directory, applied by CI/CD,
console disabled.)
