# CareLoop — operational entry points (spec §22.1, §28, §30).
# Everything runs via Docker; no local Python/npm required.

COMPOSE     := docker compose
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml

.PHONY: help up dev down seed reset-demo rebuild-graph eval eval-baseline smoke logs

help:
	@echo "CareLoop targets:"
	@echo "  make up            - start the full stack (production-style images)"
	@echo "  make dev           - start with bind mounts + hot reload (uvicorn --reload / next dev)"
	@echo "  make down          - stop the stack (volumes preserved)"
	@echo "  make seed          - (re)seed synthetic patients, facts, evidence, analytics"
	@echo "  make reset-demo    - golden reset: wipe app + Langfuse volumes, re-up, re-seed, warm trace"
	@echo "  make rebuild-graph - replay the Neo4j projection from authoritative Postgres (spec 17)"
	@echo "  make eval          - run the eval suite + launch-criteria table (spec 22)"
	@echo "  make eval-baseline - eval run that also refreshes the named baseline experiment"
	@echo "  make smoke         - real third-party smoke tests: OpenAI / Deepgram / Langfuse (spec 33)"
	@echo "  make logs          - tail logs for the whole stack"

up:
	$(COMPOSE) up -d

dev:
	$(COMPOSE_DEV) up -d

down:
	$(COMPOSE) down

seed:
	$(COMPOSE) exec backend python -m app.context.seed

# Golden demo-ready snapshot (spec §30). Recreates ALL named volumes — app data is
# re-seeded, and the empty Langfuse Postgres volume makes LANGFUSE_INIT_* headless
# init re-run — then regenerates ONE warm trace so the observability tab is never
# empty. Langfuse v3+ ingestion is async: the warm trace may take a little while to
# appear in the Langfuse UI after this completes (see infra/langfuse/README.md).
reset-demo:
	$(COMPOSE) down -v --remove-orphans
	$(COMPOSE) up -d --wait --wait-timeout 600
	$(COMPOSE) exec backend python -m app.context.seed
	$(COMPOSE) exec backend python -m app.observability.warm_trace

# Fast between-rehearsals reset: wipes encounter-derived state, keeps the
# seeded chart, replays the Neo4j projection. Full golden snapshot = reset-demo.
reset-runtime:
	$(COMPOSE) exec backend python -m app.context.reset_runtime

rebuild-graph:
	$(COMPOSE) exec backend python -m app.context.rebuild_graph

eval:
	$(COMPOSE) exec backend python -m app.evals.run

eval-baseline:
	$(COMPOSE) exec backend python -m app.evals.run --baseline

smoke:
	$(COMPOSE) exec backend python -m app.smoke

logs:
	$(COMPOSE) logs -f --tail=100
