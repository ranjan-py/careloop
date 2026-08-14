"""Structured care-plan generation (spec §12 step 5, §13).

ONE model call produces ~4 schema-validated candidate actions from the fact
inventory + finalized transcript. Everything the model must not be trusted
with is deterministic post-processing:

- ``patient_facts_used`` ids are validated server-side against the injected
  inventory; unknown ids are dropped (spec §10).
- ``evidence_refs`` are GENUINELY retrieval-produced (spec §11): BM25 over
  title+rationale, top-2 above the score floor — empty is allowed and honest,
  never decorative.
- Permission tiers are assigned by DETERMINISTIC rules (contracts v2 /
  spec §13): medication-category actions -> required_clinician_decision;
  exactly one low-risk action -> auto_demo (prefer the patient-instructions
  /"other" action); everything else -> clinician_review.
- Strategy note (spec §13): the prompt requires information-gathering /
  process-oriented actions only — never dose changes. A deterministic
  dose-language detector is exported for the verify script and evaluators.

Traced as the ``generate_care_plan`` span (with a ``context_assembly``
child, spec §20) on the persisted encounter trace id.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from app.ai import client as ai_client
from app.ai import retrieval
from app.ai.extraction import FactView, estimate_tokens
from app.ai.prompts import PromptRegistry, get_registry
from app.ai.retrieval import DEFAULT_SCORE_FLOOR, EvidenceIndex
from app.config import get_settings
from app.observability.tracing import EncounterTracer, record_usage
from app.schemas.core import ActionCategory, PermissionTier, RiskLevel

logger = logging.getLogger(__name__)

PROMPT_NAME = "careloop_care_plan"

MAX_ACTIONS = 5
MIN_ACTIONS = 1
EVIDENCE_TOP_K = 2

_RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

#: Deterministic dose-change language detector (strategy note, spec §13).
#: Matches directive verbs near an explicit milligram amount.
DOSE_CHANGE_RE = re.compile(
    r"\b(increas\w*|decreas\w*|titrat\w*|doubl\w*|halv\w*|raise|reduce|"
    r"start\w*|restart\w*|resume|switch\w*|prescrib\w*|add\w*)\b"
    r"[^.;\n]{0,60}?\b\d+(?:\.\d+)?\s*(?:mg|milligrams?)\b",
    re.IGNORECASE,
)


def find_dose_change_language(text: str) -> str | None:
    """Return the offending fragment when text contains a dose-change
    instruction (directive verb + explicit mg amount), else None."""
    match = DOSE_CHANGE_RE.search(text)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Model-facing schema (validated by responses.parse before anything persists)
# ---------------------------------------------------------------------------


class ModelAction(BaseModel):
    category: ActionCategory
    title: str
    description: str
    rationale: str
    patient_facts_used: list[str]
    risk_level: RiskLevel


class CarePlanDraft(BaseModel):
    actions: list[ModelAction]


# ---------------------------------------------------------------------------
# The candidate the caller persists (ids/status minted by the router/store)
# ---------------------------------------------------------------------------


class CandidateAction(BaseModel):
    """A generated action ready for persistence as a pending CarePlanAction."""

    category: ActionCategory
    title: str
    description: str
    rationale: str
    patient_facts_used: list[str] = Field(default_factory=list)  # validated fact ids
    evidence_refs: list[str] = Field(default_factory=list)  # retrieval-produced
    risk_level: RiskLevel
    permission: PermissionTier = "clinician_review"
    # provenance metadata for Decision rows / traces (spec §14)
    model_version: str | None = None
    prompt_version: str | None = None


# ---------------------------------------------------------------------------
# Deterministic post-processing (pure — unit-tested)
# ---------------------------------------------------------------------------


def validate_fact_ids(
    raw_ids: list[str], valid_ids: set[str]
) -> tuple[list[str], list[str]]:
    """Constrain model-referenced fact ids (spec §10): returns (kept, dropped).

    Order preserved, duplicates removed; unknown ids are dropped, never
    persisted."""
    kept: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for fact_id in raw_ids:
        if fact_id in seen:
            continue
        seen.add(fact_id)
        if fact_id in valid_ids:
            kept.append(fact_id)
        else:
            dropped.append(fact_id)
    return kept, dropped


def assign_permission_tiers(actions: list[CandidateAction]) -> None:
    """Contracts-v2 tier rules, in place:

    - medication category -> required_clinician_decision (hard stop tier).
    - exactly ONE low-risk action -> auto_demo: prefer a low-risk
      patient-instructions/"other" action; else any low-risk action; else the
      lowest-risk non-medication action. Medication actions never get it.
    - everything else -> clinician_review (the default decision path).
    """
    for action in actions:
        action.permission = (
            "required_clinician_decision"
            if action.category == "medication"
            else "clinician_review"
        )
    eligible = [a for a in actions if a.category != "medication"]
    if not eligible:
        logger.warning("No non-medication action available for the auto_demo tier.")
        return
    pick = next((a for a in eligible if a.category == "other" and a.risk_level == "low"), None)
    if pick is None:
        pick = next((a for a in eligible if a.risk_level == "low"), None)
    if pick is None:
        pick = min(eligible, key=lambda a: _RISK_ORDER[a.risk_level])
        logger.warning(
            "No low-risk action available — auto_demo assigned to lowest-risk %r (%s).",
            pick.title, pick.risk_level,
        )
    pick.permission = "auto_demo"


def attach_evidence(
    actions: list[CandidateAction],
    *,
    index: EvidenceIndex | None = None,
    k: int = EVIDENCE_TOP_K,
    score_floor: float = DEFAULT_SCORE_FLOOR,
) -> None:
    """Fill each action's evidence_refs from REAL retrieval over its
    title+rationale (spec §11). Above-floor hits only; empty is honest."""
    idx = index if index is not None else retrieval.get_index()
    for action in actions:
        hits = idx.retrieve(f"{action.title}. {action.rationale}", k, score_floor=score_floor)
        action.evidence_refs = [hit.id for hit in hits]


def build_care_plan_input(inventory: list[FactView], transcript_text: str) -> str:
    lines = "\n".join(f.inventory_line() for f in inventory) or "(no facts on record)"
    return (
        "FULL FACT INVENTORY:\n"
        f"{lines}\n\n"
        "FINALIZED ENCOUNTER TRANSCRIPT:\n"
        f"{transcript_text}"
    )


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


async def generate_care_plan(
    encounter_id: str,
    patient_id: str,
    facts: list[FactView],
    transcript_text: str,
    trace_id: str | None,
    usage_sink: dict | None = None,
    *,
    registry: PromptRegistry | None = None,
    tracer: EncounterTracer | None = None,
    evidence_index: EvidenceIndex | None = None,
) -> list[CandidateAction]:
    """ONE model call -> validated, evidence-grounded, tier-assigned actions.

    The caller (care-plan orchestration) mints ids and persists rows; free-form
    model output never mutates state directly (spec §10)."""
    registry = registry or get_registry()
    tracer = tracer or EncounterTracer(trace_id)
    settings = get_settings()

    prompt = await registry.aget(PROMPT_NAME)
    input_text = build_care_plan_input(facts, transcript_text)
    valid_ids = {f.id for f in facts}

    with tracer.span(
        "generate_care_plan",
        prompt_version=prompt.version,
        metadata={"encounter_id": encounter_id, "patient_id": patient_id},
    ) as span:
        with tracer.context_assembly(
            task="generate_care_plan",
            fact_ids=sorted(valid_ids),
            token_estimate=estimate_tokens(input_text),
        ):
            pass

        usage: dict = usage_sink if usage_sink is not None else {}
        draft = await ai_client.call_model(
            task="generate_care_plan",
            instructions=prompt.compile(),
            input_text=input_text,
            output_schema=CarePlanDraft,
            prompt_version=prompt.version,
            usage_sink=usage,
            # Guarantee-carrying generation runs on the stronger model
            # (measured: nano left 2/5 actions ungrounded).
            model=get_settings().openai_generation_model,
        )
        record_usage(span, usage)

        if len(draft.actions) < MIN_ACTIONS:
            raise ValueError(
                f"Care-plan generation returned {len(draft.actions)} actions for "
                f"encounter {encounter_id} — nothing to review (honest failure, no fallback)."
            )
        model_actions = draft.actions[:MAX_ACTIONS]
        if len(draft.actions) > MAX_ACTIONS:
            logger.warning(
                "Care plan returned %d actions — keeping the first %d.",
                len(draft.actions), MAX_ACTIONS,
            )

        actions: list[CandidateAction] = []
        all_dropped: list[str] = []
        for raw in model_actions:
            kept, dropped = validate_fact_ids(raw.patient_facts_used, valid_ids)
            all_dropped.extend(dropped)
            actions.append(
                CandidateAction(
                    category=raw.category,
                    title=raw.title.strip(),
                    description=raw.description.strip(),
                    rationale=raw.rationale.strip(),
                    patient_facts_used=kept,
                    risk_level=raw.risk_level,
                    model_version=usage.get("model") or settings.openai_model,
                    prompt_version=prompt.version,
                )
            )
        if all_dropped:
            logger.warning(
                "Dropped unknown fact ids from care-plan output: %s", all_dropped
            )

        attach_evidence(actions, index=evidence_index)
        assign_permission_tiers(actions)

        for action in actions:
            fragment = find_dose_change_language(
                f"{action.title} {action.description} {action.rationale}"
            )
            if fragment:
                logger.warning(
                    "Care-plan action %r contains dose-change language (%r) — "
                    "flagged for evaluators; clinician review still gates it.",
                    action.title, fragment,
                )

        span.update(
            output={
                "actions": [a.model_dump() for a in actions],
                "dropped_unknown_fact_ids": all_dropped,
            }
        )
    return actions
