"""The §9 incremental AI state machine — router-agnostic.

Deterministic trigger rules (no load-bearing adjectives):
1. interim text never reaches this module (UI only).
2. ``feed_final_segment`` buffers each FINALIZED utterance.
3. the fact extractor fires when >= ``min_words`` (25) words OR
   >= ``max_buffer_seconds`` (10 s) of finalized text has accumulated,
   debounced ``debounce_seconds`` (3 s) after the last finalized segment.
4. extraction diff updates working state (Postgres + Neo4j projection via
   the SAME project_facts code path as the seed).
5. the next-best-question generator runs ONLY when the fact diff is
   non-empty, with per-encounter dedup + cooldown (app.ai.suggestions).
6. EVERY trigger decision (fired/skipped + reason) is logged to the
   ``trigger_log`` table (feeds §22 evals / §23 analytics).

The WS layer hooks the async callbacks ``on_fact(fact, change)`` /
``on_suggestion(suggestion)`` / ``on_suggestion_remove(id)`` later; envelope
models come from app.schemas.core. Clock and sleep are injectable so tests
are fully deterministic (fake clock, instant debounce).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from app.ai import extraction
from app.ai.extraction import FactChanges, FactView, NewFact
from app.ai.prompts import PromptRegistry, get_registry
from app.ai.suggestions import ActiveSuggestionView, SuggestionEngine, SuggestionRow
from app.db.models import new_id
from app.observability.tracing import EncounterTracer
from app.schemas.core import Fact, TranscriptSegment

logger = logging.getLogger(__name__)

FactCallback = Callable[[Fact, str], Any]
SuggestionCallback = Callable[[Any], Any]
RemoveCallback = Callable[[str], Any]


async def _maybe_await(result: Any) -> None:
    if inspect.isawaitable(result):
        await result


# ---------------------------------------------------------------------------
# Config + trigger policy (pure, fake-clock friendly)
# ---------------------------------------------------------------------------


@dataclass
class PipelineConfig:
    min_words: int = 25
    max_buffer_seconds: float = 10.0
    debounce_seconds: float = 3.0
    # 45 s: with detached (never-cancelled) cycles, a 15 s cooldown produced
    # 15-18 suggestions per encounter — churn the spec (§8C/§9) forbids.
    suggestion_cooldown_seconds: float = 45.0
    max_active_suggestions: int = 2
    context_tail_chars: int = 600  # already-processed transcript tail kept for context


@dataclass
class TriggerDecision:
    fire: bool
    reason: str
    buffered_words: int
    buffered_seconds: float


class TriggerPolicy:
    """Pure §9 threshold logic over an injectable monotonic clock."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.buffered_words = 0
        self.first_buffered_at: float | None = None

    def note_segment(self, text: str, now: float) -> None:
        self.buffered_words += len(text.split())
        if self.first_buffered_at is None:
            self.first_buffered_at = now

    def buffered_seconds(self, now: float) -> float:
        return 0.0 if self.first_buffered_at is None else max(0.0, now - self.first_buffered_at)

    def evaluate(self, now: float, *, force: bool = False) -> TriggerDecision:
        words = self.buffered_words
        age = self.buffered_seconds(now)
        if words == 0:
            return TriggerDecision(False, "empty_buffer", words, age)
        if force:
            return TriggerDecision(True, "forced_flush", words, age)
        if words >= self.config.min_words:
            return TriggerDecision(True, f"word_threshold ({words} >= {self.config.min_words})", words, age)
        if age >= self.config.max_buffer_seconds:
            return TriggerDecision(True, f"time_threshold ({age:.1f}s >= {self.config.max_buffer_seconds:.0f}s)", words, age)
        return TriggerDecision(
            False,
            f"below_threshold ({words} words < {self.config.min_words}, {age:.1f}s < {self.config.max_buffer_seconds:.0f}s)",
            words,
            age,
        )

    def reset(self) -> None:
        self.buffered_words = 0
        self.first_buffered_at = None


@dataclass
class TriggerLogEntry:
    encounter_id: str
    stage: str  # extraction | suggestions
    decision: str  # fired | skipped
    reason: str
    buffered_words: int
    buffered_seconds: float
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Store seam — Postgres+Neo4j in prod, in-memory for tests / --no-db verify
# ---------------------------------------------------------------------------


class PipelineStore(Protocol):
    async def load_facts(self, patient_id: str) -> list[FactView]: ...

    async def add_facts(self, rows: list[NewFact], minted_ids: list[str], patient_id: str) -> None: ...

    async def update_fact_value(self, fact_id: str, value: str, confidence: float) -> None: ...

    async def mark_fact_disputed(self, fact_id: str) -> None: ...

    async def load_suggestion_state(
        self, encounter_id: str
    ) -> tuple[set[str], list[ActiveSuggestionView]]: ...

    async def add_suggestions(self, rows: list[SuggestionRow]) -> None: ...

    async def supersede_suggestions(self, suggestion_ids: list[str]) -> None: ...

    async def log_trigger(self, entry: TriggerLogEntry) -> None: ...


class InMemoryStore:
    """Persistence stub for tests and scripts/verify_ai_core.py --no-db."""

    def __init__(self) -> None:
        self.facts: dict[str, FactView] = {}
        self.fact_meta: dict[str, NewFact] = {}
        self.patient_facts: dict[str, list[str]] = {}
        self.suggestions: dict[str, SuggestionRow] = {}
        self.trigger_log: list[TriggerLogEntry] = []
        self._order = 0.0

    def seed_fact(self, patient_id: str, view: FactView) -> None:
        self.facts[view.id] = view
        self.patient_facts.setdefault(patient_id, []).append(view.id)

    async def load_facts(self, patient_id: str) -> list[FactView]:
        return [replace(self.facts[i]) for i in self.patient_facts.get(patient_id, [])]

    async def add_facts(self, rows: list[NewFact], minted_ids: list[str], patient_id: str) -> None:
        for row, fact_id in zip(rows, minted_ids):
            view = FactView(
                id=fact_id,
                fact_type=row.fact_type,
                subject=row.subject,
                value=row.value,
                source_type=row.source_type,
                source_class=row.source_class,
                verification_status=row.verification_status,
                method=row.method,
                encounter_id=row.encounter_id,
                conflicts_with=row.conflicts_with,
            )
            self.seed_fact(patient_id, view)
            self.fact_meta[fact_id] = row

    async def update_fact_value(self, fact_id: str, value: str, confidence: float) -> None:
        if fact_id in self.facts:
            self.facts[fact_id].value = value

    async def mark_fact_disputed(self, fact_id: str) -> None:
        if fact_id in self.facts:
            self.facts[fact_id].verification_status = "disputed"

    async def load_suggestion_state(
        self, encounter_id: str
    ) -> tuple[set[str], list[ActiveSuggestionView]]:
        keys = {
            s.dedup_key
            for s in self.suggestions.values()
            if s.encounter_id == encounter_id and s.dedup_key
        }
        active = [
            ActiveSuggestionView(
                id=s.id, dedup_key=s.dedup_key, created_order=s.created_at.timestamp(), text=s.text
            )
            for s in self.suggestions.values()
            if s.encounter_id == encounter_id and s.status == "active"
        ]
        return keys, active

    async def add_suggestions(self, rows: list[SuggestionRow]) -> None:
        for row in rows:
            self.suggestions[row.id] = row

    async def supersede_suggestions(self, suggestion_ids: list[str]) -> None:
        for sid in suggestion_ids:
            if sid in self.suggestions:
                self.suggestions[sid].status = "superseded"

    async def log_trigger(self, entry: TriggerLogEntry) -> None:
        self.trigger_log.append(entry)


class DatabaseStore:
    """Authoritative Postgres writes + idempotent Neo4j projection (spec §17).

    Neo4j failures degrade honestly (logged; Postgres stays committed — spec
    §2.4); replay via `make rebuild-graph` reconverges the projection.
    """

    def __init__(self, session_factory: Any = None) -> None:
        # Lazy: resolved on FIRST USE so the factory binds to the loop the
        # store actually runs on (the AI pipeline loop — app.ai.executor),
        # never the loop the store object was constructed on.
        self._session_factory_arg = session_factory

    @property
    def _session_factory(self) -> Any:
        if self._session_factory_arg is not None:
            return self._session_factory_arg
        from app.db.session import get_session_factory

        return get_session_factory()

    async def _project(self, orm_facts: list[Any]) -> None:
        from app.context.projection import ProjectionError, project_facts

        try:
            await project_facts(orm_facts)
        except ProjectionError as exc:
            logger.warning("Neo4j projection degraded (Postgres committed): %s", exc)

    async def load_facts(self, patient_id: str) -> list[FactView]:
        from sqlalchemy import select

        from app.db import models as m

        async with self._session_factory() as session:
            rows = (
                (await session.execute(select(m.Fact).where(m.Fact.patient_id == patient_id)))
                .scalars()
                .all()
            )
        return [
            FactView(
                id=r.id,
                fact_type=r.fact_type,
                subject=r.subject,
                value=r.value,
                source_type=r.source_type,
                source_class=r.source_class,
                verification_status=r.verification_status,
                method=r.method,
                encounter_id=r.encounter_id,
                conflicts_with=r.conflicts_with,
            )
            for r in rows
        ]

    async def add_facts(self, rows: list[NewFact], minted_ids: list[str], patient_id: str) -> None:
        from app.db import models as m

        orm_rows = [
            m.Fact(
                id=fact_id,
                patient_id=patient_id,
                encounter_id=row.encounter_id,
                fact_type=row.fact_type,
                subject=row.subject,
                value=row.value,
                source_type=row.source_type,
                source_class=row.source_class,
                method=row.method,
                reported_at=row.reported_at,
                confidence=row.confidence,
                verification_status=row.verification_status,
                conflicts_with=row.conflicts_with,
            )
            for row, fact_id in zip(rows, minted_ids)
        ]
        async with self._session_factory() as session:
            session.add_all(orm_rows)
            await session.commit()
        await self._project(orm_rows)

    async def update_fact_value(self, fact_id: str, value: str, confidence: float) -> None:
        from app.db import models as m

        async with self._session_factory() as session:
            row = await session.get(m.Fact, fact_id)
            if row is None:
                return
            row.value = value
            row.confidence = confidence
            await session.commit()
            await self._project([row])

    async def mark_fact_disputed(self, fact_id: str) -> None:
        from app.db import models as m

        async with self._session_factory() as session:
            row = await session.get(m.Fact, fact_id)
            if row is None:
                return
            row.verification_status = "disputed"
            await session.commit()
            await self._project([row])

    async def load_suggestion_state(
        self, encounter_id: str
    ) -> tuple[set[str], list[ActiveSuggestionView]]:
        from sqlalchemy import select

        from app.db import models as m

        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(m.Suggestion).where(m.Suggestion.encounter_id == encounter_id)
                    )
                )
                .scalars()
                .all()
            )
        keys = {r.dedup_key for r in rows if r.dedup_key}
        active = [
            ActiveSuggestionView(
                id=r.id,
                dedup_key=r.dedup_key,
                created_order=r.created_at.timestamp() if r.created_at else 0.0,
                text=r.text,
            )
            for r in rows
            if r.status == "active"
        ]
        return keys, active

    async def add_suggestions(self, rows: list[SuggestionRow]) -> None:
        from app.db import models as m

        async with self._session_factory() as session:
            session.add_all(
                m.Suggestion(
                    id=r.id,
                    encounter_id=r.encounter_id,
                    kind=r.kind,
                    text=r.text,
                    rationale=r.rationale,
                    status=r.status,
                    dedup_key=r.dedup_key,
                    created_at=r.created_at,
                )
                for r in rows
            )
            await session.commit()

    async def supersede_suggestions(self, suggestion_ids: list[str]) -> None:
        from app.db import models as m

        async with self._session_factory() as session:
            for sid in suggestion_ids:
                row = await session.get(m.Suggestion, sid)
                if row is not None and row.status == "active":
                    row.status = "superseded"
            await session.commit()

    async def log_trigger(self, entry: TriggerLogEntry) -> None:
        from app.db import models as m

        async with self._session_factory() as session:
            session.add(
                m.TriggerLog(
                    id=new_id("trg"),
                    encounter_id=entry.encounter_id,
                    stage=entry.stage,
                    decision=entry.decision,
                    reason=entry.reason,
                    buffered_words=entry.buffered_words,
                    buffered_seconds=entry.buffered_seconds,
                    detail=entry.detail,
                )
            )
            await session.commit()


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


class EncounterPipeline:
    """One instance per live encounter. Feed finalized segments; it does the
    rest (§9 triggers -> extraction -> persistence -> suggestions)."""

    def __init__(
        self,
        *,
        encounter_id: str,
        patient_id: str,
        store: PipelineStore | None = None,
        trace_id: str | None = None,
        tracer: EncounterTracer | None = None,
        registry: PromptRegistry | None = None,
        config: PipelineConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_fact: FactCallback | None = None,
        on_suggestion: SuggestionCallback | None = None,
        on_suggestion_remove: RemoveCallback | None = None,
    ) -> None:
        self.encounter_id = encounter_id
        self.patient_id = patient_id
        self.store: PipelineStore = store if store is not None else DatabaseStore()
        self.config = config or PipelineConfig()
        self.tracer = tracer or EncounterTracer(trace_id)
        self.registry = registry or get_registry()
        self.clock = clock
        self.sleep = sleep
        self.on_fact = on_fact
        self.on_suggestion = on_suggestion
        self.on_suggestion_remove = on_suggestion_remove

        self.policy = TriggerPolicy(self.config)
        self.suggestion_engine = SuggestionEngine(
            encounter_id=encounter_id,
            registry=self.registry,
            tracer=self.tracer,
            cooldown_seconds=self.config.suggestion_cooldown_seconds,
            max_active=self.config.max_active_suggestions,
        )

        self._buffer: list[TranscriptSegment] = []
        self._processed_tail = ""
        self._debounce_task: asyncio.Task | None = None
        self._cycle_task: asyncio.Task | None = None
        self._closed = False
        self.last_error: str | None = None
        self.stats: dict[str, int] = {"cycles": 0, "facts_added": 0, "suggestions_added": 0}

    # -- feeding -----------------------------------------------------------

    async def feed_final_segment(self, segment: TranscriptSegment) -> None:
        """Buffer one FINALIZED segment and (re)arm the debounce timer."""
        if self._closed:
            return
        self._buffer.append(segment)
        self.policy.note_segment(segment.text, self.clock())
        self._arm_debounce()

    def _arm_debounce(self) -> None:
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounce_then_evaluate())

    async def _debounce_then_evaluate(self) -> None:
        try:
            await self.sleep(self.config.debounce_seconds)
        except asyncio.CancelledError:
            return
        await self._evaluate()

    # -- trigger evaluation ------------------------------------------------

    async def _evaluate(self, *, force: bool = False) -> None:
        if self._closed:
            return
        if self._cycle_task is not None and not self._cycle_task.done():
            await self._log_trigger(
                "extraction",
                "skipped",
                "extraction_in_flight",
                self.policy.buffered_words,
                self.policy.buffered_seconds(self.clock()),
            )
            return
        decision = self.policy.evaluate(self.clock(), force=force)
        await self._log_trigger(
            "extraction",
            "fired" if decision.fire else "skipped",
            decision.reason,
            decision.buffered_words,
            decision.buffered_seconds,
        )
        if not decision.fire:
            return
        segments = list(self._buffer)
        self._buffer.clear()
        self.policy.reset()
        self._cycle_task = asyncio.create_task(self._run_cycle(segments, decision))
        if force:
            # flush(): completion is required before finalize continues.
            await self._cycle_task
        # Normal path: DETACHED. This method runs inside the debounce task,
        # which _arm_debounce CANCELS on every new final segment — awaiting
        # here forwarded that cancellation into the running cycle and killed
        # the suggestion model call mid-flight on every streaming run
        # (observed: extraction survived, suggestions never completed). The
        # cycle re-arms the debounce itself when more segments are buffered.

    async def _log_trigger(
        self, stage: str, decision: str, reason: str, words: int, seconds: float, detail: dict | None = None
    ) -> None:
        entry = TriggerLogEntry(
            encounter_id=self.encounter_id,
            stage=stage,
            decision=decision,
            reason=reason,
            buffered_words=words,
            buffered_seconds=round(seconds, 3),
            detail=detail or {},
        )
        try:
            await self.store.log_trigger(entry)
        except Exception:  # noqa: BLE001 — logging must never kill the loop
            logger.exception("trigger_log write failed")
        logger.info(
            "trigger %s/%s: %s (words=%d age=%.1fs enc=%s)",
            stage, decision, reason, words, seconds, self.encounter_id,
        )

    # -- the extraction/suggestion cycle -----------------------------------

    def _window_text(self, segments: list[TranscriptSegment]) -> str:
        lines = [f"{seg.speaker.capitalize()}: {seg.text}" for seg in segments]
        window = "\n".join(lines)
        if self._processed_tail:
            window = f"(earlier context)\n{self._processed_tail}\n(new)\n{window}"
        return window

    async def _run_cycle(self, segments: list[TranscriptSegment], decision: TriggerDecision) -> None:
        window = self._window_text(segments)
        try:
            self.stats["cycles"] += 1
            inventory = await self.store.load_facts(self.patient_id)
            candidates, _info = await extraction.run_extraction(
                transcript_window=window,
                inventory=inventory,
                registry=self.registry,
                tracer=self.tracer,
            )
            changes = extraction.compute_fact_changes(
                candidates, inventory, encounter_id=self.encounter_id
            )
            await self._apply_changes(changes, inventory, window)
        except Exception as exc:  # noqa: BLE001 — surfaced, never silently faked
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("AI cycle failed for encounter %s", self.encounter_id)
        finally:
            tail = "\n".join(f"{s.speaker.capitalize()}: {s.text}" for s in segments)
            self._processed_tail = (self._processed_tail + "\n" + tail)[-self.config.context_tail_chars :]
            # Segments that arrived mid-cycle would otherwise wait for the
            # NEXT segment to re-arm the debounce — re-arm here instead.
            if self._buffer and not self._closed:
                self._arm_debounce()

    async def _apply_changes(
        self, changes: FactChanges, inventory: list[FactView], window: str
    ) -> None:
        if changes.dropped_unknown_ids:
            logger.warning(
                "Dropped unknown fact ids from model output: %s", changes.dropped_unknown_ids
            )
        if changes.is_empty:
            await self._log_trigger(
                "suggestions", "skipped", "empty_fact_diff", 0, 0.0,
                detail={"skipped_duplicates": changes.skipped_duplicates},
            )
            return

        # Persist: new facts, updates, disputes — never overwrite (spec §8B).
        minted_ids = [new_id("fact") for _ in changes.new_facts]
        if changes.new_facts:
            await self.store.add_facts(changes.new_facts, minted_ids, self.patient_id)
            self.stats["facts_added"] += len(changes.new_facts)
        for update in changes.updates:
            await self.store.update_fact_value(update.fact_id, update.value, update.confidence)
        for fact_id in changes.disputed_ehr_fact_ids:
            await self.store.mark_fact_disputed(fact_id)

        updated_inventory = extraction.apply_changes_to_views(changes, inventory, minted_ids)
        views_by_id = {v.id: v for v in updated_inventory}
        now_wall = datetime.now(timezone.utc)

        # Emit state.fact callbacks (WS layer hooks these).
        for new, fact_id in zip(changes.new_facts, minted_ids):
            await self._emit_fact(self._to_schema_fact(new, fact_id, now_wall), "added")
        for update in changes.updates:
            view = views_by_id.get(update.fact_id)
            if view is not None:
                await self._emit_fact(self._view_to_schema_fact(view, now_wall), "updated")
        for fact_id in changes.disputed_ehr_fact_ids:
            view = views_by_id.get(fact_id)
            if view is not None:
                await self._emit_fact(self._view_to_schema_fact(view, now_wall), "disputed")

        # Suggestion generation only when the cycle produced NEW or DISPUTED
        # facts (spec §9 step 5) — pure value updates are not a state change
        # that warrants interrupting the clinician (churn control, §8C).
        if changes.new_facts or changes.disputed_ehr_fact_ids:
            await self._run_suggestions(changes, updated_inventory, minted_ids, window)
        else:
            await self._log_trigger(
                "suggestions", "skipped", "updates_only_no_new_facts", 0, 0.0
            )

    async def _run_suggestions(
        self,
        changes: FactChanges,
        updated_inventory: list[FactView],
        minted_ids: list[str],
        window: str,
    ) -> None:
        existing_keys, active = await self.store.load_suggestion_state(self.encounter_id)
        new_ids = set(minted_ids) | {u.fact_id for u in changes.updates} | set(
            changes.disputed_ehr_fact_ids
        )
        new_lines = [v.inventory_line() for v in updated_inventory if v.id in new_ids]
        try:
            outcome = await self.suggestion_engine.propose(
                inventory_lines=[v.inventory_line() for v in updated_inventory],
                new_fact_lines=new_lines,
                transcript_window=window,
                existing_keys=existing_keys,
                active=active,
                now=self.clock(),
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Suggestion generation failed for %s", self.encounter_id)
            await self._log_trigger("suggestions", "skipped", f"error: {type(exc).__name__}", 0, 0.0)
            return

        if outcome.skipped and not outcome.emitted:
            await self._log_trigger(
                "suggestions", "skipped", "; ".join(r for _, r in outcome.skipped), 0, 0.0
            )
        if not outcome.emitted:
            return

        await self._log_trigger(
            "suggestions", "fired",
            f"non_empty_fact_diff ({len(new_ids)} changed facts)", 0, 0.0,
            detail={"emitted_keys": [r.dedup_key for r in outcome.new_rows],
                    "superseded": outcome.supersede_ids,
                    "skipped": outcome.skipped},
        )
        if outcome.supersede_ids:
            await self.store.supersede_suggestions(outcome.supersede_ids)
            for sid in outcome.supersede_ids:
                if self.on_suggestion_remove is not None:
                    await _maybe_await(self.on_suggestion_remove(sid))
        await self.store.add_suggestions(outcome.new_rows)
        self.stats["suggestions_added"] += len(outcome.new_rows)
        for envelope in outcome.envelopes:
            if envelope.type == "suggestion.active" and self.on_suggestion is not None:
                await _maybe_await(self.on_suggestion(envelope.suggestion))

    # -- fact schema helpers -----------------------------------------------

    def _to_schema_fact(self, new: NewFact, fact_id: str, now_wall: datetime) -> Fact:
        return Fact(
            id=fact_id,
            fact_type=new.fact_type,  # type: ignore[arg-type]
            subject=new.subject,
            value=new.value,
            source_type=new.source_type,  # type: ignore[arg-type]
            source_class=new.source_class,  # type: ignore[arg-type]
            method=new.method,
            reported_at=new.reported_at,
            ingested_at=now_wall,
            confidence=new.confidence,
            verification_status=new.verification_status,  # type: ignore[arg-type]
            encounter_id=new.encounter_id,
            conflicts_with=new.conflicts_with,
        )

    def _view_to_schema_fact(self, view: FactView, now_wall: datetime) -> Fact:
        return Fact(
            id=view.id,
            fact_type=view.fact_type,  # type: ignore[arg-type]
            subject=view.subject,
            value=view.value,
            source_type=view.source_type,  # type: ignore[arg-type]
            source_class=view.source_class,  # type: ignore[arg-type]
            method=view.method,
            reported_at=now_wall,
            ingested_at=now_wall,
            confidence=1.0,
            verification_status=view.verification_status,  # type: ignore[arg-type]
            encounter_id=view.encounter_id,
            conflicts_with=view.conflicts_with,
        )

    async def _emit_fact(self, fact: Fact, change: str) -> None:
        if self.on_fact is not None:
            await _maybe_await(self.on_fact(fact, change))

    # -- lifecycle ---------------------------------------------------------

    async def flush(self) -> None:
        """Session end: cancel the debounce and force-run any buffered text."""
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        await self.wait_idle()
        await self._evaluate(force=True)

    async def wait_idle(self) -> None:
        """Await any in-flight debounce/extraction (verify script + tests)."""
        while True:
            task = self._cycle_task
            debounce = self._debounce_task
            pending = [t for t in (task, debounce) if t is not None and not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        if self._cycle_task is not None and not self._cycle_task.done():
            await asyncio.gather(self._cycle_task, return_exceptions=True)
        self.tracer.flush()
