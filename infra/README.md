# CareLoop infra

Docker-first: everything runnable runs via `docker compose` from the repo root
(`docker-compose.yml`, dev override in `docker-compose.dev.yml`, entry points in the
`Makefile`). This directory holds infra documentation and vendored references.

- `langfuse/` — vendoring notes for the self-hosted Langfuse stack (source commit,
  modifications, env vars, headless-init + async-ingestion caveats) and the pristine
  upstream compose file for diffing.
- `neo4j/` — reserved for Neo4j config/plugins if ever needed; Neo4j is currently
  configured entirely via environment variables in the root compose file.

## Host ports (spec §28)

| Service | Host port |
|---|---|
| frontend | 3002 |
| backend | 8002 |
| Neo4j browser / bolt | 7474 / 7687 |
| Langfuse web | 3101 |
| everything else (app-postgres, langfuse-postgres, clickhouse, redis, minio, worker) | none — internal only |

## Memory

Give Docker Desktop **12–16 GB** VM memory. Caps in place: ClickHouse container capped at
4 GB; Neo4j heap capped at 1 GB (+512 MB page cache).

## Dev loop

`make dev` layers `docker-compose.dev.yml` on top: bind mounts plus `uvicorn --reload`
(backend) and `next dev` (frontend) inside the containers — no image rebuild per change.
It assumes both images use `WORKDIR /app`; if a Dockerfile changes its layout, update the
bind-mount targets in `docker-compose.dev.yml`.

The production-style `make up` build is what demo and verification runs use. Pre-pull and
pre-build images before demo day; never `--build` on demo day (spec §28).
