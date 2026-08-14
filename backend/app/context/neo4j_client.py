"""Async Neo4j driver holder — the longitudinal context graph (spec §17).

Neo4j is a projection of Postgres; writes are idempotent MERGEs keyed on
stable ids (projection code lands with the seed/graph milestone). This module
owns the shared driver lifecycle and a failure-tolerant query helper.
"""

from __future__ import annotations

import asyncio
import logging

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import get_settings

logger = logging.getLogger(__name__)

# Drivers cached PER EVENT LOOP: the async driver's connections bind to the
# loop that uses them, and the AI pipeline runs on its own loop thread
# (app.ai.executor) — each loop gets its own driver transparently.
_drivers: dict[int, AsyncDriver] = {}


def _loop_key() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def get_driver() -> AsyncDriver:
    key = _loop_key()
    driver = _drivers.get(key)
    if driver is None:
        settings = get_settings()
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        _drivers[key] = driver
        logger.info("Created Neo4j driver for loop key %s", key)
    return driver


async def close_driver() -> None:
    """Close the CURRENT loop's driver (other loops own their closure)."""
    driver = _drivers.pop(_loop_key(), None)
    if driver is not None:
        await driver.close()


async def run_read_query(cypher: str, parameters: dict | None = None) -> list[dict]:
    """Execute a read query, returning plain dict records. Raises on failure —
    callers surface the degraded state honestly (spec §2.4), never fake data."""
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(cypher, parameters or {})
        return [record.data() async for record in result]
