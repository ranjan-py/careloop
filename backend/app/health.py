"""GET /api/health — real dependency checks, failure-tolerant reporting.

Each check actually probes the dependency (no fake "ok"), but a missing or
unreachable dependency yields "unconfigured"/"unreachable" with detail —
the endpoint itself never crashes (spec §2.4).
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter
from sqlalchemy import text

from app.config import get_settings
from app.context.neo4j_client import get_driver
from app.db.session import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SECONDS = 5.0


def _ok(detail: str = "") -> dict:
    return {"status": "ok", "detail": detail}


def _unreachable(detail: str) -> dict:
    return {"status": "unreachable", "detail": detail}


def _unconfigured(detail: str) -> dict:
    return {"status": "unconfigured", "detail": detail}


async def _check_postgres() -> dict:
    try:
        engine = get_engine()

        async def probe() -> None:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        await asyncio.wait_for(probe(), timeout=CHECK_TIMEOUT_SECONDS)
        return _ok("SELECT 1 succeeded")
    except Exception as exc:  # noqa: BLE001
        return _unreachable(str(exc))


async def _check_neo4j() -> dict:
    try:
        driver = get_driver()
        await asyncio.wait_for(driver.verify_connectivity(), timeout=CHECK_TIMEOUT_SECONDS)
        return _ok("bolt connectivity verified")
    except Exception as exc:  # noqa: BLE001
        return _unreachable(str(exc))


async def _check_langfuse() -> dict:
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return _unconfigured("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set")
    url = f"{settings.langfuse_host.rstrip('/')}/api/public/health"
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
        if response.status_code == 200:
            return _ok(f"{url} → 200")
        return _unreachable(f"{url} → HTTP {response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return _unreachable(str(exc))


async def _check_openai() -> dict:
    settings = get_settings()
    if not settings.openai_api_key:
        return _unconfigured("OPENAI_API_KEY not set")
    try:
        from app.ai.client import get_client

        client = get_client()
        model = await asyncio.wait_for(
            client.models.retrieve(settings.openai_model), timeout=CHECK_TIMEOUT_SECONDS
        )
        return _ok(f"model '{model.id}' verified")
    except Exception as exc:  # noqa: BLE001
        return _unreachable(str(exc))


async def _check_deepgram() -> dict:
    settings = get_settings()
    if not settings.deepgram_api_key:
        return _unconfigured("DEEPGRAM_API_KEY not set")
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_SECONDS) as client:
            response = await client.get(
                "https://api.deepgram.com/v1/projects",
                headers={"Authorization": f"Token {settings.deepgram_api_key}"},
            )
        if response.status_code == 200:
            return _ok("key accepted by Deepgram API")
        return _unreachable(f"Deepgram API → HTTP {response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return _unreachable(str(exc))


@router.get("/api/health")
async def health() -> dict:
    postgres, neo4j, langfuse, openai_dep, deepgram = await asyncio.gather(
        _check_postgres(),
        _check_neo4j(),
        _check_langfuse(),
        _check_openai(),
        _check_deepgram(),
    )
    deps = {
        "postgres": postgres,
        "neo4j": neo4j,
        "langfuse": langfuse,
        "openai": openai_dep,
        "deepgram": deepgram,
    }
    status = "ok" if all(d["status"] == "ok" for d in deps.values()) else "degraded"
    return {"status": status, "deps": deps}
