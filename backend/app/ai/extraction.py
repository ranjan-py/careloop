"""Per-condition subagent fact extraction (spec §9/§26, hand-rolled on purpose).

Three condition modules — hypertension, type-2 diabetes, CKD risk (John
Miller has all three) — each run a FOCUSED prompt over (recent finalized
transcript window + current fact inventory) and return schema-validated
candidate facts. A lightweight orchestrator fans the three out concurrently
(asyncio.gather) through the single call_model seam, then merges/dedupes on
a normalized subject+value key. Each subagent gets its own child trace span
under ``extract_patient_facts`` (spec §20).

Conflict detection is DETERMINISTIC post-processing (spec §8B): a candidate
medication_status contradicting an existing EHR fact creates a NEW fact
(source_class=patient_report, verification_status=unverified,
conflicts_with=<ehr fact id>) and marks the EHR fact disputed — never an
overwrite. Model-referenced fact ids are constrained: the inventory
(id + one-liner) is injected into the prompt and returned ids are validated
server-side; unknown ids are dropped (spec §10).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from pydantic import BaseModel

from app.ai import client as ai_client
from app.ai.prompts import PromptRegistry
from app.observability.tracing import EncounterTracer, record_usage
from app.schemas.core import FactType

logger = logging.getLogger(__name__)

_NORM_RE = re.compile(r"[^a-z0-9/.]+")

# Values that assert a medication is no longer being taken — the ONLY
# candidates that may contradict an active EHR medication fact (spec §8B).
_STOPPED_VALUE_RE = re.compile(
    r"\b(stopp?ed|stop taking|discontinu\w*|quit|no longer tak\w*|not taking|"
    r"came off|went off|off (?:the |his |her )?med|ran out)\b",
    re.IGNORECASE,
)

# Deterministic medication-status vocabulary — used for conflict detection.
_MED_STOPPED_TOKENS = ("stop", "discontinu", "quit", "not taking", "no longer")
_MED_ACTIVE_TOKENS = ("active", "taking", "current", "continu", "adherent")


def normalize(text: str) -> str:
    return _NORM_RE.sub(" ", text.strip().lower()).strip()


def normalize_med_status(value: str) -> str:
    v = value.strip().lower()
    if any(tok in v for tok in _MED_STOPPED_TOKENS):
        return "stopped"
    if any(tok in v for tok in _MED_ACTIVE_TOKENS):
        return "active"
    return normalize(v)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Model-facing schemas (validated by responses.parse before anything persists)
# ---------------------------------------------------------------------------


class CandidateFact(BaseModel):
    fact_type: FactType
    subject: str
    value: str
    method: str | None = None  # e.g. "pharmacy_kiosk"
    confidence: float
    source_quote: str
    contradicts_fact_id: str | None = None  # validated against inventory ids


class ConditionCandidates(BaseModel):
    candidates: list[CandidateFact]


# ---------------------------------------------------------------------------
# Existing-fact view + change plan (persistence-agnostic)
# ---------------------------------------------------------------------------


@dataclass
class FactView:
    """Minimal view of a persisted fact — what extraction logic needs."""

    id: str
    fact_type: str
    subject: str
    value: str
    source_type: str
    source_class: str
    verification_status: str
    method: str | None = None
    encounter_id: str | None = None
    conflicts_with: str | None = None

    def inventory_line(self) -> str:
        status = self.verification_status
        return f"{self.id} | {self.fact_type} | {self.subject}: {self.value} ({self.source_type}, {status})"


@dataclass
class NewFact:
    """A fact the pipeline should persist (id minted by the caller/store)."""

    fact_type: str
    subject: str
    value: str
    source_type: str
    source_class: str
    method: str | None
    reported_at: datetime
    confidence: float
    verification_status: str
    encounter_id: str
    conflicts_with: str | None
    source_quote: str = ""


@dataclass
class FactUpdate:
    fact_id: str
    value: str
    confidence: float


@dataclass
class FactChanges:
    """Deterministic diff of merged candidates against existing facts."""

    new_facts: list[NewFact] = field(default_factory=list)
    updates: list[FactUpdate] = field(default_factory=list)
    disputed_ehr_fact_ids: list[str] = field(default_factory=list)
    dropped_unknown_ids: list[str] = field(default_factory=list)
    skipped_duplicates: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.new_facts or self.updates or self.disputed_ehr_fact_ids)


# ---------------------------------------------------------------------------
# Condition subagents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConditionAgent:
    key: str  # trace span suffix: extract_<key>
    prompt_name: str


CONDITION_AGENTS: tuple[ConditionAgent, ...] = (
    ConditionAgent(key="hypertension", prompt_name="careloop_extract_hypertension"),
    ConditionAgent(key="type2_diabetes", prompt_name="careloop_extract_type2_diabetes"),
    ConditionAgent(key="ckd_risk", prompt_name="careloop_extract_ckd_risk"),
)


def build_extraction_input(inventory: list[FactView], transcript_window: str) -> str:
    lines = "\n".join(f.inventory_line() for f in inventory) or "(no facts on record)"
    return (
        "CURRENT FACT INVENTORY:\n"
        f"{lines}\n\n"
        "RECENT FINALIZED TRANSCRIPT:\n"
        f"{transcript_window}"
    )


async def _run_condition_agent(
    agent: ConditionAgent,
    *,
    input_text: str,
    registry: PromptRegistry,
    tracer: EncounterTracer,
    usage_totals: list[dict],
) -> list[CandidateFact]:
    prompt = await registry.aget(agent.prompt_name)
    with tracer.condition_subagent(agent.key, prompt_version=prompt.version) as span:
        usage: dict = {}
        parsed = await ai_client.call_model(
            task=f"extract_{agent.key}",
            instructions=prompt.compile(),
            input_text=input_text,
            output_schema=ConditionCandidates,
            prompt_version=prompt.version,
            usage_sink=usage,
        )
        record_usage(span, usage)
        if usage:
            usage_totals.append(usage)
        span.update(output={"candidates": [c.model_dump() for c in parsed.candidates]})
    return parsed.candidates


async def run_extraction(
    *,
    transcript_window: str,
    inventory: list[FactView],
    registry: PromptRegistry,
    tracer: EncounterTracer,
) -> tuple[list[CandidateFact], dict]:
    """Fan out the three condition subagents concurrently, merge candidates.

    Returns (merged candidates, run_info). A single failing subagent degrades
    (logged, others proceed); if ALL fail, the exception propagates so the
    caller can surface an honest error (spec §2.4).
    """
    input_text = build_extraction_input(inventory, transcript_window)
    usage_totals: list[dict] = []
    run_info: dict = {"agents": {}, "input_chars": len(input_text)}

    with tracer.extract_patient_facts(transcript_chars=len(transcript_window)) as root:
        with tracer.context_assembly(
            task="extract_patient_facts",
            fact_ids=[f.id for f in inventory],
            token_estimate=estimate_tokens(input_text),
        ):
            pass
        results = await asyncio.gather(
            *(
                _run_condition_agent(
                    agent,
                    input_text=input_text,
                    registry=registry,
                    tracer=tracer,
                    usage_totals=usage_totals,
                )
                for agent in CONDITION_AGENTS
            ),
            return_exceptions=True,
        )
        merged_input: list[CandidateFact] = []
        failures: list[BaseException] = []
        for agent, result in zip(CONDITION_AGENTS, results):
            if isinstance(result, BaseException):
                failures.append(result)
                run_info["agents"][agent.key] = f"FAILED: {type(result).__name__}: {result}"
                logger.warning("Condition subagent %s failed: %s", agent.key, result)
            else:
                run_info["agents"][agent.key] = f"{len(result)} candidates"
                merged_input.extend(result)
        if failures and len(failures) == len(CONDITION_AGENTS):
            raise failures[0]

        merged = merge_candidates(merged_input)
        run_info["merged_count"] = len(merged)
        run_info["usage"] = usage_totals
        root.update(output={"merged": [c.model_dump() for c in merged], "agents": run_info["agents"]})
    return merged, run_info


def merge_candidates(candidates: list[CandidateFact]) -> list[CandidateFact]:
    """Dedupe on normalized (fact_type, subject, value); keep max confidence,
    first non-null method/contradicts id. Order of first appearance kept."""
    merged: dict[tuple[str, str, str], CandidateFact] = {}
    for cand in candidates:
        value_norm = (
            normalize_med_status(cand.value)
            if cand.fact_type == "medication_status"
            else normalize(cand.value)
        )
        key = (cand.fact_type, normalize(cand.subject), value_norm)
        existing = merged.get(key)
        if existing is None:
            merged[key] = cand
            continue
        best = existing.model_copy(
            update={
                "confidence": max(existing.confidence, cand.confidence),
                "method": existing.method or cand.method,
                "contradicts_fact_id": existing.contradicts_fact_id or cand.contradicts_fact_id,
            }
        )
        merged[key] = best
    return list(merged.values())


# ---------------------------------------------------------------------------
# Deterministic diff + conflict detection (spec §8B)
# ---------------------------------------------------------------------------


def compute_fact_changes(
    candidates: list[CandidateFact],
    existing: list[FactView],
    *,
    encounter_id: str,
    now: datetime | None = None,
) -> FactChanges:
    now = now or datetime.now(timezone.utc)
    changes = FactChanges()
    valid_ids = {f.id for f in existing}
    # Per-cycle noise control: at most one NEW fact per (fact_type, subject)
    # and one update per prior fact id — highest confidence wins.
    new_by_key: dict[tuple[str, str], NewFact] = {}
    updates_by_id: dict[str, FactUpdate] = {}

    ehr_meds = {
        normalize(f.subject): f
        for f in existing
        if f.fact_type == "medication_status" and f.source_type == "ehr"
    }
    existing_keys = {
        (
            f.fact_type,
            normalize(f.subject),
            normalize_med_status(f.value) if f.fact_type == "medication_status" else normalize(f.value),
        )
        for f in existing
    }
    by_type_subject = {
        (f.fact_type, normalize(f.subject)): f
        for f in existing
        if f.source_type == "patient_report" and f.encounter_id == encounter_id
    }
    disputed: set[str] = set()

    for cand in candidates:
        cand = cand.model_copy(update={"confidence": min(1.0, max(0.0, cand.confidence))})
        subject_norm = normalize(cand.subject)
        value_norm = (
            normalize_med_status(cand.value)
            if cand.fact_type == "medication_status"
            else normalize(cand.value)
        )

        # Constrain model-referenced ids: unknown ids are dropped (spec §10).
        if cand.contradicts_fact_id and cand.contradicts_fact_id not in valid_ids:
            changes.dropped_unknown_ids.append(cand.contradicts_fact_id)
            cand = cand.model_copy(update={"contradicts_fact_id": None})

        # Exact restatement of a known fact -> no diff.
        if (cand.fact_type, subject_norm, value_norm) in existing_keys:
            changes.skipped_duplicates += 1
            continue

        # Same-encounter patient_report fact evolving -> update, not new row.
        prior = by_type_subject.get((cand.fact_type, subject_norm))
        if prior is not None:
            best = updates_by_id.get(prior.id)
            if best is None or cand.confidence > best.confidence:
                updates_by_id[prior.id] = FactUpdate(
                    fact_id=prior.id, value=cand.value, confidence=cand.confidence
                )
            existing_keys.add((cand.fact_type, subject_norm, value_norm))
            continue

        conflicts_with: str | None = None
        if cand.fact_type == "medication_status":
            ehr_fact = ehr_meds.get(subject_norm)
            # DETERMINISTIC conflict — but ONLY for a genuine status
            # contradiction (patient stopped vs EHR active). A dosage/schedule
            # elaboration ("1000 mg morning and night") is NOT a conflict
            # (observed false positive on metformin in the full-loop run).
            if (
                ehr_fact is not None
                and _STOPPED_VALUE_RE.search(cand.value)
                and "active" in normalize_med_status(ehr_fact.value)
            ):
                conflicts_with = ehr_fact.id
                if ehr_fact.verification_status != "disputed" and ehr_fact.id not in disputed:
                    disputed.add(ehr_fact.id)
                    changes.disputed_ehr_fact_ids.append(ehr_fact.id)
        if (
            conflicts_with is None
            and cand.contradicts_fact_id
            and cand.fact_type != "medication_status"
        ):
            # Validated model hint — non-medication contradictions only;
            # medication conflicts are exclusively deterministic.
            conflicts_with = cand.contradicts_fact_id

        new_fact = NewFact(
            fact_type=cand.fact_type,
            subject=cand.subject,
            value=cand.value,
            source_type="patient_report",
            source_class="patient_report",
            method=cand.method,
            reported_at=now,
            confidence=cand.confidence,
            verification_status="unverified",
            encounter_id=encounter_id,
            conflicts_with=conflicts_with,
            source_quote=cand.source_quote,
        )
        key = (cand.fact_type, subject_norm)
        kept = new_by_key.get(key)
        if kept is None or new_fact.confidence > kept.confidence:
            if kept is not None:
                # Preserve enrichments from the discarded near-duplicate.
                new_fact.method = new_fact.method or kept.method
                new_fact.conflicts_with = new_fact.conflicts_with or kept.conflicts_with
                changes.skipped_duplicates += 1
            new_by_key[key] = new_fact
        else:
            kept.method = kept.method or new_fact.method
            kept.conflicts_with = kept.conflicts_with or new_fact.conflicts_with
            changes.skipped_duplicates += 1
        existing_keys.add((cand.fact_type, subject_norm, value_norm))

    changes.new_facts = list(new_by_key.values())
    changes.updates = list(updates_by_id.values())
    return changes


def apply_changes_to_views(
    changes: FactChanges, existing: list[FactView], minted_ids: list[str]
) -> list[FactView]:
    """Pure helper: existing views + changes -> updated inventory (for the
    in-memory store and for building the post-diff suggestion context)."""
    views = [replace(f) for f in existing]
    by_id = {f.id: f for f in views}
    for fact_id in changes.disputed_ehr_fact_ids:
        if fact_id in by_id:
            by_id[fact_id].verification_status = "disputed"
    for update in changes.updates:
        if update.fact_id in by_id:
            by_id[update.fact_id].value = update.value
    for new, fact_id in zip(changes.new_facts, minted_ids):
        views.append(
            FactView(
                id=fact_id,
                fact_type=new.fact_type,
                subject=new.subject,
                value=new.value,
                source_type=new.source_type,
                source_class=new.source_class,
                verification_status=new.verification_status,
                method=new.method,
                encounter_id=new.encounter_id,
                conflicts_with=new.conflicts_with,
            )
        )
    return views
