"""Pre-decision encounter summary (spec §12 step 4).

ONE model call over (fact inventory + finalized transcript) produces a short
clinician-facing summary of what happened this visit — patient-reported
information, chart conflicts, remaining uncertainty — generated BEFORE any
care-plan decision exists, so it must never contain a plan. Follows the
report prompt-family pattern (``careloop_encounter_summary`` in
PROMPT_DEFAULTS, published to Langfuse prompt management on first use).

The orchestrator (POST /api/encounters/{id}/end) wires this in as step 4 and
persists the result on ``Encounter.summary``. Traced as the
``finalize_encounter_summary`` span (with a ``context_assembly`` child) on
the persisted encounter trace id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel

from app.ai import client as ai_client
from app.ai.extraction import FactView, estimate_tokens
from app.ai.prompts import PromptRegistry, get_registry
from app.config import get_settings
from app.observability.tracing import EncounterTracer, record_usage

logger = logging.getLogger(__name__)

PROMPT_NAME = "careloop_encounter_summary"
SPAN_NAME = "finalize_encounter_summary"


class SummaryOutput(BaseModel):
    """Schema-validated model output — free-form text never persists raw."""

    title: str
    body: str


@dataclass(frozen=True)
class GeneratedSummary:
    title: str
    body: str
    prompt_version: str | None
    model_version: str | None

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}" if self.title.strip() else self.body


def build_summary_input(facts: list[FactView], transcript_text: str) -> str:
    fact_lines = "\n".join(f.inventory_line() for f in facts) or "(no facts on record)"
    return (
        "PATIENT FACTS (chart + this visit, with provenance):\n"
        f"{fact_lines}\n\n"
        "FINALIZED ENCOUNTER TRANSCRIPT:\n"
        f"{transcript_text or '(no transcript captured)'}"
    )


async def generate_encounter_summary(
    encounter_id: str,
    patient_id: str,
    facts: list[FactView],
    transcript_text: str,
    trace_id: str | None,
    usage_sink: dict | None = None,
    *,
    registry: PromptRegistry | None = None,
    tracer: EncounterTracer | None = None,
) -> GeneratedSummary:
    """ONE model call -> validated pre-decision summary.

    Raises on provider failure — the caller surfaces the error honestly
    (spec §2.4); there is no canned-summary fallback.
    """
    registry = registry or get_registry()
    tracer = tracer or EncounterTracer(trace_id)
    settings = get_settings()

    prompt = await registry.aget(PROMPT_NAME)
    input_text = build_summary_input(facts, transcript_text)
    fact_ids = [f.id for f in facts]

    with tracer.span(
        SPAN_NAME,
        prompt_version=prompt.version,
        metadata={"encounter_id": encounter_id, "patient_id": patient_id},
    ) as span:
        with tracer.context_assembly(
            task=SPAN_NAME, fact_ids=fact_ids, token_estimate=estimate_tokens(input_text)
        ):
            pass

        usage: dict = usage_sink if usage_sink is not None else {}
        parsed = await ai_client.call_model(
            task=SPAN_NAME,
            instructions=prompt.compile(),
            input_text=input_text,
            output_schema=SummaryOutput,
            prompt_version=prompt.version,
            usage_sink=usage,
        )
        record_usage(span, usage)
        span.update(output={"title": parsed.title, "body": parsed.body})

    # Degeneration guard (shared with reports — observed gpt-5-nano emitting
    # trailing brace-runs + meta-commentary inside string fields).
    from app.ai.reports import sanitize_generated_text

    title, title_issues = sanitize_generated_text(parsed.title)
    body, body_issues = sanitize_generated_text(parsed.body)
    if title_issues or body_issues:
        logger.warning(
            "Encounter summary output degeneration (sanitized): %s",
            title_issues + body_issues,
        )
    return GeneratedSummary(
        title=title,
        body=body,
        prompt_version=prompt.version,
        model_version=usage.get("model") or settings.openai_model,
    )
