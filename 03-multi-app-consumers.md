# Many Applications, One Hasura — Consumer Onboarding and What IaC Provisions

> Run the simulation: `python consumers/simulate.py` (stdlib only, no installs).
> Prereq: pipeline has run once, JWT auth enabled (both done if you followed `01`/`02`).

## The use case

Several independent applications query the same Hasura endpoint, each needing a
different slice of data, and none of them holding the admin secret:

| App | Hasura role | Credential | What it may see |
|---|---|---|---|
| `ops-monitor` | `ops` | JWT (role claim) | `_export_runs` only — pipeline health, nothing business |
| `city-dashboard` | `analyst` | JWT (role claim) | the three marts, aggregates enabled |
| `kiosk-manhattan` | `kiosk` | JWT (role + `x-hasura-borough` claim) | `taxi_zones`, 3 columns, **rows filtered to its borough** |
| (anonymous) | — | none | rejected outright |

Key mechanics demonstrated by `consumers/simulate.py`:

- **JWT auth mode**: Hasura verifies each request's `Authorization: Bearer` token
  against `HASURA_GRAPHQL_JWT_SECRET` (HS256 + shared key locally; in production an
  IdP — Auth0/Cognito/Keycloak — signs RS256 and Hasura fetches keys from a JWKS URL.
  Same claims, different signer).
- **Role from the token**: `x-hasura-default-role` in the claims decides which
  permission set applies. Apps cannot escalate — the schema each role sees simply
  doesn't contain the other fields (`field not found`, not `permission denied`).
- **Row-level security from claims**: the `kiosk` permission filter is
  `borough = X-Hasura-Borough`, a *session variable* read from the JWT. Ten kiosks in
  ten boroughs are ten tokens — one role, zero query changes, zero new permissions.
- **Column-level security**: `kiosk` sees 3 of 4 columns on the one table it can read.

## What "a new need" means, and what gets provisioned

Every new need is one of four shapes. For each: what we did locally, and what
Terraform/Ansible would own in production.

### 1. New consumer app, existing data (most common)

Local steps (this repo): create role permissions via metadata API → commit
`hasura/metadata.json` → hand the app a JWT.

| Artifact | Locally | Production provisioning |
|---|---|---|
| Identity/credential | hand-minted JWT | **Terraform**: IdP resources (Auth0 client / Cognito app client / Keycloak client), client secret into Secret Manager |
| Role + permissions | metadata API call | **git + CI** (`hasura metadata apply`): metadata is code, reviewed like code — Terraform generally should *not* own Hasura metadata |
| Rate limits / depth limits per role | n/a locally (EE/Cloud feature) | metadata (CI) or **Terraform** if using Hasura Cloud provider |
| Monitoring | n/a | **Terraform**: per-app dashboards, alert policies, log-based metrics on role/operation name |

### 2. New data shape (new mart/aggregation)

Local steps: new DDL in `exporter/sql/bootstrap.sql` + transform in `transform.sql`
→ run exporter → track table + permissions in metadata.

| Artifact | Locally | Production provisioning |
|---|---|---|
| Table DDL | idempotent bootstrap in exporter | **migration tooling in CI** (hasura migrations, Flyway, dbt) — *not* Terraform; TF is poor at DDL lifecycles |
| Pipeline change | edit transform SQL | same repo, CI deploys new job image — **Terraform** owns the job definition (Cloud Run job / ECS task / Airflow DAG bucket), **Ansible** if it's a VM cron |
| Tracking + permissions | metadata API | git + CI as above |

### 3. New data source (another database feeding Hasura)

| Artifact | Locally | Production provisioning |
|---|---|---|
| The database | another compose service | **Terraform**: Cloud SQL/RDS instance, replicas, backups, network peering/private IP |
| Credentials | `.env` | **Terraform**: Secret Manager entries + IAM bindings; Hasura reads via `from_env` |
| Hasura source registration | `pg_add_source` metadata call | metadata in git + CI; the env var it references is provisioned by Terraform |

### 4. Scale/reliability need

| Artifact | Production provisioning |
|---|---|
| More Hasura replicas, CPU/mem | **Terraform** (ECS/Cloud Run/K8s manifests or Helm values) |
| Connection pooling (per-source pool size, pgbouncer) | **Terraform** for the infra, metadata for per-source pool settings |
| Read replicas for analytics traffic | **Terraform**: replica + Hasura read-replica routing env vars |
| OS patching, docker upgrades, cert renewal (self-hosted VMs) | **Ansible** playbooks |

## The general rule of thumb

- **Terraform** owns *long-lived infrastructure and access*: databases, the Hasura
  runtime, IdP clients, secrets, IAM, networking, alerting. Declarative, stateful,
  reviewed rarely.
- **Ansible** owns *machine configuration and orchestrated procedures* when
  self-hosting on VMs: installing docker, templating env files, rolling restarts,
  running one-off playbooks (migrations, metadata apply). If you're fully on managed
  services + CI, you may not need Ansible at all.
- **Neither owns Hasura metadata or SQL migrations.** Those live in git next to the
  application code and are applied by CI (`hasura metadata apply`, migration runner)
  — they change too often and diff too poorly to sit in Terraform state. This repo's
  `hasura/metadata.json` is the local stand-in for that.
- **Per new consumer app**, the recurring provisioning bundle is:
  **credential (TF) + role/permissions (metadata in CI) + limits (metadata/TF) +
  observability (TF)** — and nothing else should need to change.

## Local artifact → production analog map

| This repo | Production |
|---|---|
| `.env` | Secret Manager / Vault (Terraform-managed) |
| `docker-compose.yml` services | Terraform service definitions (Cloud Run/ECS/K8s) |
| `HASURA_GRAPHQL_JWT_SECRET` (HS256 key) | IdP + JWKS URL (Terraform-managed IdP resources) |
| `consumers/simulate.py` minting JWTs | apps obtaining tokens from the IdP (client-credentials flow) |
| `hasura/metadata.json` + curl apply | hasura CLI metadata dir applied by CI/CD |
| `exporter/sql/bootstrap.sql` | versioned SQL migrations in CI |
| `docker compose run exporter` | orchestrator-scheduled job (Terraform-defined) |
