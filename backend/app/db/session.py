"""Async engine / session for the app Postgres (authoritative store, spec §17).

Alembic is intentionally skipped for this demo: init_db() runs create_all on
startup (acceptable per the demo brief). Startup stays failure-tolerant —
an unreachable database is reported by /api/health, not a crash loop.

Engines/factories are cached PER EVENT LOOP: asyncpg connections are bound to
the loop that acquires them, and the AI pipeline runs on its own loop thread
(app.ai.executor) so realtime audio traffic can never starve model/DB work.
Each loop transparently gets its own engine + pool.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.db.models import Base

logger = logging.getLogger(__name__)

_engines: dict[int, AsyncEngine] = {}
_factories: dict[int, async_sessionmaker[AsyncSession]] = {}


def _loop_key() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0  # sync context — resources created here bind on first use


def get_engine() -> AsyncEngine:
    key = _loop_key()
    engine = _engines.get(key)
    if engine is None:
        engine = create_async_engine(
            get_settings().app_database_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
        )
        _engines[key] = engine
        _factories[key] = async_sessionmaker(engine, expire_on_commit=False)
        logger.info("Created app-postgres engine for loop key %s", key)
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    key = _loop_key()
    if key not in _factories:
        get_engine()
    return _factories[key]


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with get_session_factory()() as session:
        yield session


async def init_db() -> bool:
    """Create tables if the database is reachable. Returns True on success."""
    try:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("App Postgres schema ensured (create_all).")
        return True
    except Exception as exc:  # noqa: BLE001 — startup must not crash; health reports truthfully
        logger.warning("App Postgres unavailable at startup: %s", exc)
        return False


async def dispose_engine() -> None:
    """Dispose the CURRENT loop's engine (other loops own their disposal)."""
    key = _loop_key()
    engine = _engines.pop(key, None)
    _factories.pop(key, None)
    if engine is not None:
        await engine.dispose()
