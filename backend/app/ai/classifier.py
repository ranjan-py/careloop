"""Rejection-category suggestion (spec §14) — a SMALL classification call.

Maps the clinician's free-text rejection reason onto the SINGLE shared
RejectionCategory enum (schemas.core — never redefined here). Runs on the
cheap OPENAI_EVAL_MODEL. The suggestion only pre-selects the modal's
dropdown: the clinician's own choice stays authoritative, and
"Feedback captured. No automatic model change is made."

Traced as the ``classify_override`` span (spec §20) on the encounter trace.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from app.ai import client as ai_client
from app.ai.prompts import PromptRegistry, get_registry
from app.config import get_settings
from app.observability.tracing import EncounterTracer, record_usage
from app.schemas.core import RejectionCategory

logger = logging.getLogger(__name__)

PROMPT_NAME = "careloop_rejection_classifier"


class RejectionSuggestion(BaseModel):
    """Schema-validated classifier output — constrained to the shared enum."""

    category: RejectionCategory


async def suggest_rejection_category(
    free_text: str,
    *,
    action_title: str | None = None,
    trace_id: str | None = None,
    registry: PromptRegistry | None = None,
    tracer: EncounterTracer | None = None,
    usage_sink: dict | None = None,
) -> RejectionCategory:
    """Suggest a RejectionCategory for the clinician's free-text reason.

    Advisory only — the caller must treat the clinician-selected category as
    authoritative (spec §14). Raises on empty input or provider failure; the
    caller degrades honestly (no pre-selection) rather than guessing."""
    if not free_text.strip():
        raise ValueError("Cannot classify an empty rejection reason.")

    registry = registry or get_registry()
    tracer = tracer or EncounterTracer(trace_id)
    settings = get_settings()

    prompt = await registry.aget(PROMPT_NAME)
    input_text = (
        (f"REJECTED ACTION: {action_title}\n" if action_title else "")
        + f"CLINICIAN'S FREE-TEXT REASON:\n{free_text.strip()}"
    )

    with tracer.span(
        "classify_override",
        prompt_version=prompt.version,
        metadata={"model": settings.openai_eval_model},
    ) as span:
        usage: dict = usage_sink if usage_sink is not None else {}
        parsed = await ai_client.call_model(
            task="classify_override",
            instructions=prompt.compile(),
            input_text=input_text,
            output_schema=RejectionSuggestion,
            model=settings.openai_eval_model,
            prompt_version=prompt.version,
            usage_sink=usage,
        )
        record_usage(span, usage)
        span.update(output={"suggested_category": parsed.category.value})

    logger.info("Rejection-category suggestion: %s", parsed.category.value)
    return parsed.category
