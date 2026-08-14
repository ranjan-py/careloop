# Self-hosted Langfuse — vendoring notes (spec §19)

## What was vendored, from where

The Langfuse services in the root `docker-compose.yml` (`langfuse-web`, `langfuse-worker`,
`langfuse-postgres`, `clickhouse`, `redis`, `minio`) are vendored from the **official**
Langfuse self-hosted Docker Compose:

- Source file: <https://github.com/langfuse/langfuse/blob/main/docker-compose.yml>
  (linked from <https://langfuse.com/self-hosting/deployment/docker-compose>)
- Commit: `351890a0086626e5e025be31cc0f00ca6cf352d3` (main, 2026-08-12,
  "fix(docker): pass LLM connection whitelist env vars (#16014)")
- Image tags at that commit: `langfuse/langfuse:4`, `langfuse/langfuse-worker:4`,
  `clickhouse/clickhouse-server:25.12`, `redis:7`, `postgres:17` (we pin 17 explicitly),
  `cgr.dev/chainguard/minio`
- A pristine, unmodified copy is kept next to this file as
  [`upstream-docker-compose.yml`](./upstream-docker-compose.yml) — diff it against the
  Langfuse block in the root compose to audit every change.

## Modifications vs upstream (each tagged `# MODIFIED:` in docker-compose.yml)

1. **Host ports stripped** for every Langfuse service except `langfuse-web`, which is
   published as `3100:3000` (frontend owns host 3000). Worker 3030, ClickHouse 8123/9000,
   MinIO 9090/9091, Redis 6379, and Langfuse Postgres 5432 are all internal-only.
2. **Service rename:** upstream `postgres` → `langfuse-postgres`, to avoid confusion with
   the app's own `app-postgres`. `DATABASE_URL` updated accordingly, with a dedicated
   `langfuse`/`langfuse` user/db.
3. **Env vars wired to `.env` names** (see table below) instead of upstream's
   `POSTGRES_PASSWORD` / `SALT` / `ENCRYPTION_KEY` / generic names.
4. **Healthchecks added** for `langfuse-web` (`GET /api/public/ready`) and
   `langfuse-worker` (`GET /api/health` on internal port 3030); upstream ships none for
   these two. Generous `start_period` (120s) because first boot runs Postgres +
   ClickHouse migrations.
5. **ClickHouse memory cap**: `deploy.resources.limits.memory: 4G` (spec §28). ClickHouse
   is cgroup-aware and self-limits below the container cap.
6. **`TELEMETRY_ENABLED=false`** — no phoning home from the demo box.
7. **`NEXTAUTH_URL=http://localhost:3100`** — matches the remapped host port.
8. **Browser-facing MinIO media endpoint** (`LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT` on web)
   points at in-network `http://minio:9000` instead of upstream's host-exposed
   `http://localhost:9090`. Consequence: media attachments on traces would not be
   viewable from the host browser — this demo does not upload trace media, so nothing
   is lost. Batch export stays disabled, same as upstream.
9. **Hardcoded non-secret constants** (bucket names, prefixes, region `auto`, redis
   host/port, S3 access key id `minio`) instead of upstream's `${VAR:-default}`
   passthroughs — fewer knobs, same values.

## Environment variables consumed (all defined in `.env.example`)

| `.env` variable | Used by | Purpose |
|---|---|---|
| `LANGFUSE_POSTGRES_PASSWORD` | langfuse-postgres, web/worker `DATABASE_URL` | Langfuse's own Postgres password |
| `CLICKHOUSE_PASSWORD` | clickhouse, web/worker | ClickHouse auth (user `clickhouse`) |
| `REDIS_AUTH` | redis (`--requirepass`), web/worker | Redis password |
| `MINIO_ROOT_PASSWORD` | minio, web/worker S3 secret keys | Blob-store credentials (user `minio`) |
| `LANGFUSE_SALT` | web/worker `SALT` | API-key hashing salt |
| `LANGFUSE_ENCRYPTION_KEY` | web/worker `ENCRYPTION_KEY` | Optional; MUST be 64 hex chars if set (`openssl rand -hex 32`). Left empty in `.env.example`; the compose falls back to an all-zero local-demo-only key so an empty value never breaks boot. |
| `NEXTAUTH_SECRET` | langfuse-web | Langfuse UI session signing |
| `LANGFUSE_INIT_ORG_ID/_ORG_NAME/_PROJECT_ID/_PROJECT_NAME/_PROJECT_PUBLIC_KEY/_PROJECT_SECRET_KEY/_USER_EMAIL/_USER_NAME/_USER_PASSWORD` | langfuse-web | Headless first-boot initialization (below) |
| `LANGFUSE_INGESTION_QUEUE_DELAY_MS`, `LANGFUSE_INGESTION_CLICKHOUSE_WRITE_INTERVAL_MS` | web/worker | Optional ingestion tuning; empty = Langfuse defaults |
| `LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES`, `LANGFUSE_LLM_CONNECTION_WHITELISTED_*` | web/worker | Upstream passthroughs; unset = defaults |

The backend client uses `LANGFUSE_HOST=http://langfuse-web:3000` (in-network) plus
`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`, which are pinned to the **same** values as
`LANGFUSE_INIT_PROJECT_PUBLIC_KEY`/`LANGFUSE_INIT_PROJECT_SECRET_KEY`.

No additional variables had to be appended to `.env.example` — everything the official
stack requires beyond that file's Langfuse block is a non-secret constant hardcoded in
the compose file.

## Caveat 1 — headless init only runs on FIRST boot (spec §19)

`LANGFUSE_INIT_*` (org, project, user, pinned `pk-lf-…`/`sk-lf-…` API keys) is applied by
`langfuse-web` **only when it boots against an empty database volume**
(`langfuse_postgres_data`). Editing those values later does nothing; the stack will keep
whatever was initialized first.

- To force re-init: `make reset-demo` — it runs `docker compose down -v`, which deletes
  the Langfuse volumes, so the next `up` re-runs headless init cleanly, then re-seeds the
  app and regenerates one warm trace.
- Keys must stay in sync: `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (backend client)
  must equal `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`/`LANGFUSE_INIT_PROJECT_SECRET_KEY`
  (init), otherwise the backend gets 401s from a freshly reset Langfuse.
- Langfuse UI login after reset: `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`.

## Caveat 2 — v3+ ingestion is asynchronous (spec §19)

Ingestion flows web → blob store (MinIO) / queue (Redis) → worker → ClickHouse. A trace
accepted with HTTP 207 can take **seconds or longer** to become visible in the Langfuse
UI, especially right after a cold start while migrations/queues warm up. Therefore:

- Generate the encounter trace EARLY in any demo; do not end a segment with "and now the
  trace" seconds after producing it.
- The in-app Trace Summary tab is driven from the app's **own Postgres** records
  (`GET /api/ops/trace-summary/...`), NOT live Langfuse queries — it must never block on
  ingestion lag.
- `make reset-demo` finishes by regenerating one warm trace so the Langfuse project is
  never empty post-reset; allow a beat for it to appear.

## Memory

Budget 12–16 GB of Docker Desktop VM memory for the full stack (spec §28). ClickHouse is
capped at 4 GB here and the Neo4j heap at 1 GB (`NEO4J_server_memory_heap_max__size`) in
the app section of the compose file.
