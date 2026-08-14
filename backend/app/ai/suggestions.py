"""Next-best-question generator (spec §8C/§9) — triggered ONLY on a
non-empty fact diff by the pipeline.

Inputs: updated fact inventory + last transcript window (+ active
suggestions so the model never repeats them). Output is a schema-validated
list of {kind, text, rationale, dedup_key} which is then filtered by a
DETERMINISTIC policy:

- dedup: a dedup_key that already exists for THIS encounter (any status) is
  never re-emitted — keys are scoped PER ENCOUNTER, never per patient
  (rehearsals must not suppress the live run's scripted suggestion).
- cooldown: after any emission, further emissions are suppressed for
  ~15-20 s (default 15) — skips are logged as trigger decisions.
- max 2 active: emitting beyond the cap supersedes the OLDEST active
  suggestion (status -> superseded, plus a suggestion.remove envelope).

The module returns Suggestion rows to persist and the WS envelopes to emit;
the caller (pipeline/WS layer) owns persistence and delivery.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import BaseModel

from app.ai import client as ai_client
from app.ai.prompts import PromptRegistry
from app.db.models import new_id
from app.observability.tracing import EncounterTracer, record_usage
from app.schemas.core import (
    Suggestion,
    SuggestionActiveMessage,
    SuggestionKind,
    SuggestionRemoveMessage,
)

logger = logging.getLogger(__name__)

PROMPT_NAME = "careloop_next_best_question"

_KEY_RE = re.compile(r"[^a-z0-9]+")


def normalize_dedup_key(raw: str) -> str:
    key = _KEY_RE.sub("_", raw.strip().lower()).strip("_")
    return key[:200] or "unkeyed"


# ---------------------------------------------------------------------------
# Model-facing schema
# ---------------------------------------------------------------------------


class SuggestionCandidate(BaseModel):
    kind: SuggestionKind
    text: str
    rationale: str
    dedup_key: str


class NextBestSuggestions(BaseModel):
    suggestions: list[SuggestionCandidate]


# ---------------------------------------------------------------------------
# Deterministic policy state (per encounter)
# ---------------------------------------------------------------------------


@dataclass
class ActiveSuggestionView:
    id: str
    dedup_key: str | None
    created_order: float  # monotonic ordering key (emission time)
    text: str = ""


@dataclass
class SuggestionRow:
    """Persistence-ready Suggestion row (matches db.models.Suggestion)."""

    id: str
    encounter_id: str
    kind: str
    text: str
    rationale: str
    status: str
    dedup_key: str
    created_at: datetime


@dataclass
class SuggestionOutcome:
    new_rows: list[SuggestionRow] = field(default_factory=list)
    supersede_ids: list[str] = field(default_factory=list)
    envelopes: list[BaseModel] = field(default_factory=list)  # ServerMessage members
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (dedup_key, reason)
    prompt_version: str | None = None
    usage: dict = field(default_factory=dict)

    @property
    def emitted(self) -> bool:
        return bool(self.new_rows)


class SuggestionEngine:
    """Per-encounter next-best-question engine. The model proposes; the
    deterministic policy (dedup / cooldown / max-active) disposes."""

    def __init__(
        self,
        *,
        encounter_id: str,
        registry: PromptRegistry,
        tracer: EncounterTracer,
        cooldown_seconds: float = 15.0,
        max_active: int = 2,
        max_suggestions_per_run: int = 2,
    ) -> None:
        self.encounter_id = encounter_id
        self.registry = registry
        self.tracer = tracer
        self.cooldown_seconds = cooldown_seconds
        self.max_active = max_active
        self.max_suggestions_per_run = max_suggestions_per_run
        self._last_emit_at: float | None = None  # monotonic

    # -- prompt context ----------------------------------------------------

    @staticmethod
    def build_input(
        *,
        inventory_lines: list[str],
        new_fact_lines: list[str],
        transcript_window: str,
        active_texts: list[str],
        used_keys: set[str],
    ) -> str:
        inventory = "\n".join(inventory_lines) or "(no facts on record)"
        new_facts = "\n".join(f"NEW {line}" for line in new_fact_lines) or "(none)"
        active = "\n".join(f"- {t}" for t in active_texts) or "(none)"
        used = ", ".join(sorted(used_keys)) or "(none)"
        return (
            f"FACT INVENTORY:\n{inventory}\n\n"
            f"FACTS EXTRACTED JUST NOW (priority):\n{new_facts}\n\n"
            f"RECENT FINALIZED TRANSCRIPT:\n{transcript_window}\n\n"
            f"CURRENTLY ACTIVE SUGGESTIONS (do not repeat):\n{active}\n\n"
            f"DEDUP KEYS ALREADY USED THIS ENCOUNTER (do not reuse): {used}"
        )

    # -- main entry --------------------------------------------------------

    async def propose(
        self,
        *,
        inventory_lines: list[str],
        new_fact_lines: list[str],
        transcript_window: str,
        existing_keys: set[str],
        active: list[ActiveSuggestionView],
        now: float,
        wall_now: datetime | None = None,
    ) -> SuggestionOutcome:
        """One generation run. ``now`` is a monotonic clock value (injectable
        for tests); ``existing_keys``/``active`` come from the store."""
        outcome = SuggestionOutcome()

        # Global cooldown gate — checked BEFORE spending a model call.
        if (
            self._last_emit_at is not None
            and (now - self._last_emit_at) < self.cooldown_seconds
        ):
            outcome.skipped.append(
                ("*", f"cooldown ({now - self._last_emit_at:.1f}s < {self.cooldown_seconds:.0f}s)")
            )
            return outcome

        prompt = await self.registry.aget(PROMPT_NAME)
        outcome.prompt_version = prompt.version
        input_text = self.build_input(
            inventory_lines=inventory_lines,
            new_fact_lines=new_fact_lines,
            transcript_window=transcript_window,
            active_texts=[a.text for a in active if a.text],
            used_keys={normalize_dedup_key(k) for k in existing_keys},
        )

        with self.tracer.generate_next_best_action(prompt_version=prompt.version) as span:
            usage: dict = {}
            parsed = await ai_client.call_model(
                task="generate_next_best_action",
                instructions=prompt.compile(max_suggestions=self.max_suggestions_per_run),
                input_text=input_text,
                output_schema=NextBestSuggestions,
                prompt_version=prompt.version,
                usage_sink=usage,
            )
            record_usage(span, usage)
            outcome.usage = usage

            self._apply_policy(parsed.suggestions, existing_keys, active, now, wall_now, outcome)
            span.update(
                output={
                    "emitted": [r.dedup_key for r in outcome.new_rows],
                    "superseded": outcome.supersede_ids,
                    "skipped": outcome.skipped,
                }
            )
        return outcome

    # -- deterministic policy ---------------------------------------------

    def _apply_policy(
        self,
        candidates: list[SuggestionCandidate],
        existing_keys: set[str],
        active: list[ActiveSuggestionView],
        now: float,
        wall_now: datetime | None,
        outcome: SuggestionOutcome,
    ) -> None:
        wall = wall_now or datetime.now(timezone.utc)
        seen_keys = {normalize_dedup_key(k) for k in existing_keys}
        active_pool = sorted(active, key=lambda a: a.created_order)

        for cand in candidates[: self.max_suggestions_per_run]:
            key = normalize_dedup_key(cand.dedup_key)
            if key in seen_keys:
                outcome.skipped.append((key, "dedup_key already used this encounter"))
                continue

            row = SuggestionRow(
                id=new_id("sug"),
                encounter_id=self.encounter_id,
                kind=cand.kind,
                text=cand.text.strip(),
                rationale=cand.rationale.strip(),
                status="active",
                dedup_key=key,
                created_at=wall,
            )

            # Max-active cap: supersede the OLDEST active suggestion.
            while len(active_pool) + 1 > self.max_active and active_pool:
                oldest = active_pool.pop(0)
                outcome.supersede_ids.append(oldest.id)
                outcome.envelopes.append(SuggestionRemoveMessage(suggestion_id=oldest.id))

            outcome.new_rows.append(row)
            outcome.envelopes.append(
                SuggestionActiveMessage(
                    suggestion=Suggestion(
                        id=row.id,
                        encounter_id=row.encounter_id,
                        kind=row.kind,  # type: ignore[arg-type]
                        text=row.text,
                        rationale=row.rationale,
                        created_at=row.created_at,
                        status="active",  # type: ignore[arg-type]
                    )
                )
            )
            seen_keys.add(key)
            active_pool.append(ActiveSuggestionView(id=row.id, dedup_key=key, created_order=now))
            self._last_emit_at = now
