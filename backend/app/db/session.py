"""Async engine / session for the app Postgres (authoritative store, spec §17).

Alembic is intentionally skipped for this demo: init_db() runs create_all on
startup (acceptable per the demo brief). Startup stays failure-tolerant —
an unreachable database is reported by /api/health, not a crash loop.
"""

from __future__ import annotations

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

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(
            get_settings().app_database_url,
            echo=False,
            pool_pre_ping=True,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


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
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
