"""Thin provider-seam AI client (spec §10).

ALL model calls in the app route through call_model() — provider/model are
configuration, prompts and schemas stay provider-portable. OpenAI is the
runtime; the seam is the point. No other module may import the openai SDK.

Anti-fake rule (spec §2.1): if no API key is configured, calls raise
ProviderNotConfiguredError — there is no canned-response fallback.
"""

from __future__ import annotations

import logging
from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import get_settings

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_client: AsyncOpenAI | None = None


class ProviderNotConfiguredError(RuntimeError):
    """Raised when an AI call is attempted without a configured provider key."""


def get_client() -> AsyncOpenAI:
    global _client
    settings = get_settings()
    if not settings.openai_api_key:
        raise ProviderNotConfiguredError(
            "OPENAI_API_KEY is not set — AI features are BLOCKED (real integration not verified)."
        )
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def call_model(
    *,
    task: str,
    instructions: str,
    input_text: str,
    output_schema: type[SchemaT],
    model: str | None = None,
    prompt_version: str | None = None,
) -> SchemaT:
    """Single internal wrapper around the provider (Responses API, structured output).

    - task: short task name — becomes the trace span identity (spec §20).
    - prompt_version: Langfuse prompt-management version identifier; recorded
      on traces and persisted rows once observability wiring lands.
    - output_schema: Pydantic model; the response is schema-validated before
      any caller may persist it (free-form model text never mutates state).
    """
    settings = get_settings()
    client = get_client()
    chosen_model = model or settings.openai_model
    logger.info("call_model task=%s model=%s prompt_version=%s", task, chosen_model, prompt_version)
    response = await client.responses.parse(
        model=chosen_model,
        instructions=instructions,
        input=input_text,
        text_format=output_schema,
    )
    parsed = response.output_parsed
    if parsed is None:
        raise ValueError(f"Model returned no parseable output for task '{task}'")
    return parsed


async def verify_model() -> dict:
    """Startup model verification (spec §10): confirm the configured model
    exists behind the real key. Failure-tolerant — returns a status dict and
    never crashes startup; /api/health surfaces the result."""
    settings = get_settings()
    if not settings.openai_api_key:
        return {
            "status": "unconfigured",
            "detail": "OPENAI_API_KEY not set — BLOCKED, real integration not verified.",
            "model": settings.openai_model,
        }
    try:
        client = get_client()
        model = await client.models.retrieve(settings.openai_model)
        logger.info("OpenAI model verified: %s", model.id)
        return {"status": "ok", "detail": f"Model '{model.id}' verified.", "model": model.id}
    except Exception as exc:  # noqa: BLE001 — report, don't crash
        logger.warning("OpenAI model verification failed: %s", exc)
        return {"status": "unreachable", "detail": str(exc), "model": settings.openai_model}
