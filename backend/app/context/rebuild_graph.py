"""Replay the Neo4j projection from authoritative Postgres —
`python -m app.context.rebuild_graph` / `make rebuild-graph` (spec §17).

Wipes ONLY careloop-labeled nodes, then MERGEs every committed Postgres row
back through the same projection helpers the seed and the AI loop use.
Postgres is never touched. Exit 0 on success, 1 on any failure (reported
honestly — no silent partial state).
"""

from __future__ import annotations

import asyncio
import sys

from app.context.neo4j_client import close_driver
from app.context.projection import ProjectionError, replay_from_postgres
from app.db.session import dispose_engine, get_session_factory, init_db


async def rebuild() -> int:
    if not await init_db():
        print("rebuild-graph FAILED: app Postgres unreachable.", file=sys.stderr)
        return 1
    try:
        async with get_session_factory()() as session:
            counts = await replay_from_postgres(session, wipe=True)
    except ProjectionError as exc:
        print(f"rebuild-graph FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — e.g. Postgres read failure mid-replay
        print(f"rebuild-graph FAILED reading Postgres: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        f"Graph projection rebuilt from Postgres: {counts['nodes']} nodes, "
        f"{counts['relationships']} relationships."
    )
    return 0


async def _main() -> int:
    try:
        return await rebuild()
    finally:
        await close_driver()
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
