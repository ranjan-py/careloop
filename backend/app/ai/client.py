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

# Clients cached PER EVENT LOOP: httpx connections bind to the loop that uses
# them, and the AI pipeline runs on its own loop thread (app.ai.executor).
_clients: dict[int, AsyncOpenAI] = {}


class ProviderNotConfiguredError(RuntimeError):
    """Raised when an AI call is attempted without a configured provider key."""


def _loop_key() -> int:
    import asyncio

    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def get_client() -> AsyncOpenAI:
    settings = get_settings()
    if not settings.openai_api_key:
        raise ProviderNotConfiguredError(
            "OPENAI_API_KEY is not set — AI features are BLOCKED (real integration not verified)."
        )
    key = _loop_key()
    client = _clients.get(key)
    if client is None:
        # Per-request timeout + one retry: a single hung HTTP request must
        # never freeze the live encounter loop (observed in full-loop run:
        # one stalled call blocked the cycle task for the whole session).
        client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=45.0, max_retries=1)
        _clients[key] = client
    return client


async def call_model(
    *,
    task: str,
    instructions: str,
    input_text: str,
    output_schema: type[SchemaT],
    model: str | None = None,
    prompt_version: str | None = None,
    usage_sink: dict | None = None,
    reasoning_effort: str | None = None,
) -> SchemaT:
    """Single internal wrapper around the provider (Responses API, structured output).

    - task: short task name — becomes the trace span identity (spec §20).
    - prompt_version: Langfuse prompt-management version identifier; recorded
      on traces and persisted rows once observability wiring lands.
    - output_schema: Pydantic model; the response is schema-validated before
      any caller may persist it (free-form model text never mutates state).
    - usage_sink: optional dict the caller provides; on success it is filled
      with {model, input_tokens, output_tokens, total_tokens} so tracing can
      record token usage/cost (spec §20) without changing the return type.
    """
    import time as _time

    settings = get_settings()
    client = get_client()
    chosen_model = model or settings.openai_model
    logger.info("call_model task=%s model=%s prompt_version=%s", task, chosen_model, prompt_version)
    started = _time.monotonic()
    extra: dict = {}
    effort = reasoning_effort or settings.openai_reasoning_effort
    if effort:
        # GPT-5 family: effort caps reasoning-token spend. Measured on nano:
        # default ~21 s/call vs low ~4 s — load-bearing for the live loop.
        # Per-task override: suggestions run "minimal" (short-output task;
        # p95 was 10.2 s at "low" with the full fact-inventory input).
        extra["reasoning"] = {"effort": effort}
    try:
        response = await client.responses.parse(
            model=chosen_model,
            instructions=instructions,
            input=input_text,
            text_format=output_schema,
            **extra,
        )
    except Exception as exc:
        logger.warning(
            "call_model task=%s FAILED after %.1fs: %s: %s",
            task, _time.monotonic() - started, type(exc).__name__, exc,
        )
        raise
    logger.info("call_model task=%s done in %.1fs", task, _time.monotonic() - started)
    parsed = response.output_parsed
    if parsed is None:
        raise ValueError(f"Model returned no parseable output for task '{task}'")
    if usage_sink is not None:
        usage = getattr(response, "usage", None)
        usage_sink["model"] = chosen_model
        usage_sink["input_tokens"] = getattr(usage, "input_tokens", None)
        usage_sink["output_tokens"] = getattr(usage, "output_tokens", None)
        usage_sink["total_tokens"] = getattr(usage, "total_tokens", None)
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
