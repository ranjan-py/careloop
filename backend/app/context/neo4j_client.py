"""Async Neo4j driver holder — the longitudinal context graph (spec §17).

Neo4j is a projection of Postgres; writes are idempotent MERGEs keyed on
stable ids (projection code lands with the seed/graph milestone). This module
owns the shared driver lifecycle and a failure-tolerant query helper.
"""

from __future__ import annotations

import logging

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import get_settings

logger = logging.getLogger(__name__)

_driver: AsyncDriver | None = None


def get_driver() -> AsyncDriver:
    global _driver
    if _driver is None:
        settings = get_settings()
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _driver


async def close_driver() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def run_read_query(cypher: str, parameters: dict | None = None) -> list[dict]:
    """Execute a read query, returning plain dict records. Raises on failure —
    callers surface the degraded state honestly (spec §2.4), never fake data."""
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(cypher, parameters or {})
        return [record.data() async for record in result]
