"""AI-loop core tests (spec §9/§8B/§26) — mocked providers (spec §2.3).

Covers: trigger thresholds/debounce determinism (fake clock), the lisinopril
conflict-detection case, unknown-fact-id dropping, and orchestrator merge.
Suggestion dedup/cooldown live in tests/test_ai_suggestions.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ai import extraction
from app.ai.extraction import (
    CandidateFact,
    ConditionCandidates,
    FactView,
    compute_fact_changes,
    merge_candidates,
)
from app.ai.pipeline import (
    EncounterPipeline,
    InMemoryStore,
    PipelineConfig,
    TriggerPolicy,
)
from app.ai.prompts import PromptRegistry
from app.ai.suggestions import NextBestSuggestions, SuggestionCandidate
from app.observability.tracing import EncounterTracer
from app.schemas.core import TranscriptSegment

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Shared fixtures / fakes
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def ehr_facts() -> list[FactView]:
    return [
        FactView(
            id="med-lisinopril",
            fact_type="medication_status",
            subject="lisinopril",
            value="active",
            source_type="ehr",
            source_class="synthea_ehr",
            verification_status="confirmed",
        ),
        FactView(
            id="med-metformin",
            fact_type="medication_status",
            subject="metformin",
            value="active",
            source_type="ehr",
            source_class="synthea_ehr",
            verification_status="confirmed",
        ),
        FactView(
            id="obs-potassium",
            fact_type="lab",
            subject="Potassium",
            value="4.1 mmol/L",
            source_type="ehr",
            source_class="synthea_ehr",
            verification_status="confirmed",
        ),
    ]


def lisinopril_stopped(confidence: float = 0.9, contradicts: str | None = "med-lisinopril") -> CandidateFact:
    return CandidateFact(
        fact_type="medication_status",
        subject="lisinopril",
        value="stopped",
        method=None,
        confidence=confidence,
        source_quote="I actually stopped taking the lisinopril about two weeks ago.",
        contradicts_fact_id=contradicts,
    )


def kiosk_bp() -> CandidateFact:
    return CandidateFact(
        fact_type="observation",
        subject="Blood pressure",
        value="150/95 mmHg",
        method="pharmacy_kiosk",
        confidence=0.85,
        source_quote="Last week it said about one fifty over ninety five.",
        contradicts_fact_id=None,
    )


def registry_fallback_only() -> PromptRegistry:
    """A registry that never touches the network (client=None -> fallback)."""

    class _NoClient(PromptRegistry):
        def _client(self):  # noqa: D401
            return None

    return _NoClient()


def make_mock_call_model(responses: dict[str, object]):
    """call_model replacement keyed by task name; unknown tasks return empty."""
    calls: list[dict] = []

    async def _mock(*, task, instructions, input_text, output_schema, model=None,
                    prompt_version=None, usage_sink=None):
        calls.append({"task": task, "input_text": input_text, "prompt_version": prompt_version})
        if usage_sink is not None:
            usage_sink.update({"model": "mock", "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        if task in responses:
            return responses[task]
        if output_schema is ConditionCandidates:
            return ConditionCandidates(candidates=[])
        if output_schema is NextBestSuggestions:
            return NextBestSuggestions(suggestions=[])
        raise AssertionError(f"unexpected task {task}")

    _mock.calls = calls
    return _mock


async def instant_sleep(_seconds: float) -> None:
    return None


def make_pipeline(store: InMemoryStore, mock_call_model, clock: FakeClock,
                  config: PipelineConfig | None = None, **kwargs) -> EncounterPipeline:
    return EncounterPipeline(
        encounter_id="enc_test",
        patient_id="john-miller",
        store=store,
        tracer=EncounterTracer(None),
        registry=registry_fallback_only(),
        config=config or PipelineConfig(debounce_seconds=0.0),
        clock=clock,
        sleep=instant_sleep,
        **kwargs,
    )


def seg(text: str, speaker: str = "patient", ts: float = 0.0) -> TranscriptSegment:
    return TranscriptSegment(id=f"seg_{ts}", speaker=speaker, text=text, ts=ts)


# ---------------------------------------------------------------------------
# Trigger thresholds / debounce determinism (fake clock)
# ---------------------------------------------------------------------------


class TestTriggerPolicy:
    def test_empty_buffer_never_fires(self):
        policy = TriggerPolicy(PipelineConfig())
        decision = policy.evaluate(1000.0)
        assert not decision.fire
        assert decision.reason == "empty_buffer"

    def test_word_threshold_fires_at_25_words(self):
        policy = TriggerPolicy(PipelineConfig())
        policy.note_segment(" ".join(["word"] * 24), 1000.0)
        assert not policy.evaluate(1001.0).fire
        policy.note_segment("word", 1001.5)
        decision = policy.evaluate(1002.0)
        assert decision.fire
        assert "word_threshold" in decision.reason
        assert decision.buffered_words == 25

    def test_time_threshold_fires_at_10s_even_with_few_words(self):
        policy = TriggerPolicy(PipelineConfig())
        policy.note_segment("just a few words", 1000.0)
        assert not policy.evaluate(1009.9).fire
        decision = policy.evaluate(1010.0)
        assert decision.fire
        assert "time_threshold" in decision.reason

    def test_below_both_thresholds_skips_with_reason(self):
        policy = TriggerPolicy(PipelineConfig())
        policy.note_segment("five short words here now", 1000.0)
        decision = policy.evaluate(1003.0)
        assert not decision.fire
        assert "below_threshold" in decision.reason

    def test_reset_clears_buffer_accounting(self):
        policy = TriggerPolicy(PipelineConfig())
        policy.note_segment(" ".join(["w"] * 40), 1000.0)
        policy.reset()
        assert not policy.evaluate(2000.0).fire

    def test_force_fires_regardless_of_thresholds(self):
        policy = TriggerPolicy(PipelineConfig())
        policy.note_segment("tiny", 1000.0)
        decision = policy.evaluate(1000.1, force=True)
        assert decision.fire
        assert decision.reason == "forced_flush"


class TestPipelineTriggerLogging:
    async def test_skip_and_fire_are_both_logged(self, monkeypatch):
        store = InMemoryStore()
        clock = FakeClock()
        mock = make_mock_call_model({})
        monkeypatch.setattr(extraction.ai_client, "call_model", mock)
        pipe = make_pipeline(store, mock, clock)

        await pipe.feed_final_segment(seg("short one"))
        await pipe.wait_idle()
        assert [(e.decision, e.stage) for e in store.trigger_log] == [("skipped", "extraction")]
        assert "below_threshold" in store.trigger_log[0].reason

        await pipe.feed_final_segment(seg(" ".join(["word"] * 30)))
        await pipe.wait_idle()
        fired = [e for e in store.trigger_log if e.decision == "fired"]
        assert len(fired) == 1
        assert "word_threshold" in fired[0].reason
        # Empty extraction output -> suggestions skipped for empty diff, and logged.
        assert any(
            e.stage == "suggestions" and e.decision == "skipped" and e.reason == "empty_fact_diff"
            for e in store.trigger_log
        )
        await pipe.close()

    async def test_time_threshold_with_fake_clock(self, monkeypatch):
        store = InMemoryStore()
        clock = FakeClock()
        mock = make_mock_call_model({})
        monkeypatch.setattr(extraction.ai_client, "call_model", mock)
        pipe = make_pipeline(store, mock, clock)

        await pipe.feed_final_segment(seg("only four words here"))
        await pipe.wait_idle()
        assert store.trigger_log[-1].decision == "skipped"

        clock.advance(10.5)  # buffered text is now ">=10s old"
        await pipe.feed_final_segment(seg("more words"))
        await pipe.wait_idle()
        fired = [e for e in store.trigger_log if e.decision == "fired"]
        assert len(fired) == 1
        assert "time_threshold" in fired[0].reason
        await pipe.close()


# ---------------------------------------------------------------------------
# Orchestrator fan-out + merge
# ---------------------------------------------------------------------------


class TestOrchestratorMerge:
    def test_duplicates_across_agents_merge_keep_max_confidence(self):
        a = lisinopril_stopped(confidence=0.7, contradicts=None)
        b = lisinopril_stopped(confidence=0.95)
        b = b.model_copy(update={"value": "Stopped taking it"})  # normalizes to "stopped"
        merged = merge_candidates([a, b, kiosk_bp()])
        assert len(merged) == 2
        lis = next(c for c in merged if c.subject == "lisinopril")
        assert lis.confidence == 0.95
        assert lis.contradicts_fact_id == "med-lisinopril"  # first non-null kept

    def test_distinct_values_not_merged(self):
        merged = merge_candidates([kiosk_bp(), kiosk_bp().model_copy(update={"value": "160/100 mmHg"})])
        assert len(merged) == 2

    async def test_fan_out_runs_all_three_and_merges(self, monkeypatch):
        per_task = {
            "extract_hypertension": ConditionCandidates(candidates=[lisinopril_stopped(0.8), kiosk_bp()]),
            "extract_type2_diabetes": ConditionCandidates(candidates=[]),
            "extract_ckd_risk": ConditionCandidates(candidates=[lisinopril_stopped(0.92)]),
        }
        mock = make_mock_call_model(per_task)
        monkeypatch.setattr(extraction.ai_client, "call_model", mock)
        merged, info = await extraction.run_extraction(
            transcript_window="Patient: I stopped the lisinopril.",
            inventory=ehr_facts(),
            registry=registry_fallback_only(),
            tracer=EncounterTracer(None),
        )
        assert sorted(c["task"] for c in mock.calls) == [
            "extract_ckd_risk", "extract_hypertension", "extract_type2_diabetes",
        ]
        assert len(merged) == 2  # lisinopril deduped across agents
        lis = next(c for c in merged if c.subject == "lisinopril")
        assert lis.confidence == 0.92
        assert info["agents"]["type2_diabetes"] == "0 candidates"
        # Fact inventory (id + one-liner) injected into every prompt (spec §10).
        assert all("med-lisinopril | medication_status" in c["input_text"] for c in mock.calls)

    async def test_single_agent_failure_degrades_not_crashes(self, monkeypatch):
        async def _mock(*, task, output_schema, **kwargs):
            if task == "extract_hypertension":
                raise RuntimeError("provider blew up")
            return ConditionCandidates(candidates=[kiosk_bp()])

        monkeypatch.setattr(extraction.ai_client, "call_model", _mock)
        merged, info = await extraction.run_extraction(
            transcript_window="x",
            inventory=[],
            registry=registry_fallback_only(),
            tracer=EncounterTracer(None),
        )
        assert len(merged) == 1
        assert info["agents"]["hypertension"].startswith("FAILED")

    async def test_all_agents_failing_raises(self, monkeypatch):
        async def _mock(**kwargs):
            raise RuntimeError("all down")

        monkeypatch.setattr(extraction.ai_client, "call_model", _mock)
        with pytest.raises(RuntimeError):
            await extraction.run_extraction(
                transcript_window="x",
                inventory=[],
                registry=registry_fallback_only(),
                tracer=EncounterTracer(None),
            )


# ---------------------------------------------------------------------------
# Conflict detection — the lisinopril case (spec §8B: never overwrite)
# ---------------------------------------------------------------------------


class TestConflictDetection:
    def test_lisinopril_stopped_conflicts_with_ehr_active(self):
        changes = compute_fact_changes(
            [lisinopril_stopped()], ehr_facts(), encounter_id="enc_test", now=NOW
        )
        assert len(changes.new_facts) == 1
        new = changes.new_facts[0]
        assert new.source_type == "patient_report"
        assert new.source_class == "patient_report"
        assert new.verification_status == "unverified"
        assert new.conflicts_with == "med-lisinopril"
        assert changes.disputed_ehr_fact_ids == ["med-lisinopril"]
        # The EHR fact itself is never rewritten — only marked disputed.
        assert all(u.fact_id != "med-lisinopril" for u in changes.updates)

    def test_conflict_is_deterministic_without_model_hint(self):
        changes = compute_fact_changes(
            [lisinopril_stopped(contradicts=None)], ehr_facts(), encounter_id="enc_test", now=NOW
        )
        assert changes.new_facts[0].conflicts_with == "med-lisinopril"
        assert changes.disputed_ehr_fact_ids == ["med-lisinopril"]

    def test_agreeing_medication_status_is_not_a_conflict(self):
        cand = lisinopril_stopped().model_copy(update={"value": "active", "contradicts_fact_id": None})
        changes = compute_fact_changes([cand], ehr_facts(), encounter_id="enc_test", now=NOW)
        assert changes.is_empty
        assert changes.skipped_duplicates == 1

    def test_unknown_contradicts_fact_id_dropped(self):
        cand = kiosk_bp().model_copy(update={"contradicts_fact_id": "fact-does-not-exist"})
        changes = compute_fact_changes([cand], ehr_facts(), encounter_id="enc_test", now=NOW)
        assert changes.dropped_unknown_ids == ["fact-does-not-exist"]
        assert changes.new_facts[0].conflicts_with is None

    def test_kiosk_bp_added_with_method(self):
        changes = compute_fact_changes([kiosk_bp()], ehr_facts(), encounter_id="enc_test", now=NOW)
        assert len(changes.new_facts) == 1
        assert changes.new_facts[0].method == "pharmacy_kiosk"
        assert changes.disputed_ehr_fact_ids == []

    def test_confidence_clamped(self):
        cand = kiosk_bp().model_copy(update={"confidence": 3.5})
        changes = compute_fact_changes([cand], [], encounter_id="enc_test", now=NOW)
        assert changes.new_facts[0].confidence == 1.0

    def test_per_cycle_subject_dedup_keeps_best_and_enrichments(self):
        # Two near-duplicate BP candidates in one cycle (different phrasing,
        # same subject) -> ONE new fact, highest confidence, method preserved.
        a = kiosk_bp()  # 0.85, method pharmacy_kiosk
        b = kiosk_bp().model_copy(
            update={"value": "about 150/95 last week", "confidence": 0.9, "method": None}
        )
        changes = compute_fact_changes([a, b], [], encounter_id="enc_test", now=NOW)
        assert len(changes.new_facts) == 1
        assert changes.new_facts[0].confidence == 0.9
        assert changes.new_facts[0].method == "pharmacy_kiosk"
        assert changes.skipped_duplicates == 1

    def test_same_prior_fact_updated_once_per_cycle(self):
        prior = FactView(
            id="fact_prior",
            fact_type="symptom",
            subject="dizziness",
            value="ongoing",
            source_type="patient_report",
            source_class="patient_report",
            verification_status="unverified",
            encounter_id="enc_test",
        )
        cands = [
            CandidateFact(fact_type="symptom", subject="dizziness", value="resolved",
                          confidence=0.7, source_quote="q1"),
            CandidateFact(fact_type="symptom", subject="dizziness", value="mostly resolved",
                          confidence=0.9, source_quote="q2"),
        ]
        changes = compute_fact_changes(cands, [prior], encounter_id="enc_test", now=NOW)
        assert changes.new_facts == []
        assert len(changes.updates) == 1
        assert changes.updates[0].value == "mostly resolved"


# ---------------------------------------------------------------------------
# End-to-end pipeline cycle (mocked model): facts persisted, EHR disputed,
# callbacks fired, suggestion generated only on non-empty diff.
# ---------------------------------------------------------------------------


class TestPipelineEndToEnd:
    async def test_full_cycle_conflict_and_suggestion(self, monkeypatch):
        store = InMemoryStore()
        for view in ehr_facts():
            store.seed_fact("john-miller", view)

        responses = {
            "extract_hypertension": ConditionCandidates(candidates=[lisinopril_stopped(), kiosk_bp()]),
            "generate_next_best_action": NextBestSuggestions(
                suggestions=[
                    SuggestionCandidate(
                        kind="question",
                        text="Confirm when the dizziness began relative to taking lisinopril.",
                        rationale="Patient stopped lisinopril due to dizziness.",
                        dedup_key="dizziness_timing_lisinopril",
                    )
                ]
            ),
        }
        mock = make_mock_call_model(responses)
        monkeypatch.setattr(extraction.ai_client, "call_model", mock)
        import app.ai.suggestions as sug_mod

        monkeypatch.setattr(sug_mod.ai_client, "call_model", mock)

        fact_events: list[tuple[str, str, str | None]] = []
        sug_events: list[str] = []

        async def on_fact(fact, change):
            fact_events.append((fact.subject, change, fact.conflicts_with))

        def on_suggestion(s):  # sync callback also supported
            sug_events.append(s.text)

        clock = FakeClock()
        pipe = make_pipeline(store, mock, clock, on_fact=on_fact, on_suggestion=on_suggestion)
        await pipe.feed_final_segment(
            seg("Well, about that. I actually stopped taking the lisinopril about two weeks "
                "ago because it was making me really dizzy in the mornings after breakfast.")
        )
        await pipe.wait_idle()

        # Facts persisted: new conflicting fact + kiosk BP; EHR fact disputed, not overwritten.
        stored = {v.subject: v for v in store.facts.values() if v.source_type == "patient_report"}
        assert stored["lisinopril"].value == "stopped"
        assert stored["lisinopril"].conflicts_with == "med-lisinopril"
        assert stored["lisinopril"].verification_status == "unverified"
        assert store.facts["med-lisinopril"].value == "active"  # never overwritten
        assert store.facts["med-lisinopril"].verification_status == "disputed"
        assert stored["Blood pressure"].method == "pharmacy_kiosk"

        # Callbacks: two added + one disputed.
        changes = sorted(c for _, c, _ in fact_events)
        assert changes == ["added", "added", "disputed"]
        disputed = next(e for e in fact_events if e[1] == "disputed")
        assert disputed[0] == "lisinopril"

        # Suggestion persisted + emitted, dedup key normalized and scoped to encounter.
        assert sug_events == ["Confirm when the dizziness began relative to taking lisinopril."]
        rows = list(store.suggestions.values())
        assert len(rows) == 1
        assert rows[0].dedup_key == "dizziness_timing_lisinopril"
        assert rows[0].encounter_id == "enc_test"
        assert rows[0].status == "active"
        await pipe.close()

    async def test_suggestions_not_called_on_empty_diff(self, monkeypatch):
        store = InMemoryStore()
        for view in ehr_facts():
            store.seed_fact("john-miller", view)
        # Extraction returns only a restatement of an existing EHR fact.
        restatement = lisinopril_stopped().model_copy(
            update={"value": "active", "contradicts_fact_id": None}
        )
        mock = make_mock_call_model(
            {"extract_hypertension": ConditionCandidates(candidates=[restatement])}
        )
        monkeypatch.setattr(extraction.ai_client, "call_model", mock)
        import app.ai.suggestions as sug_mod

        monkeypatch.setattr(sug_mod.ai_client, "call_model", mock)

        pipe = make_pipeline(store, mock, FakeClock())
        await pipe.feed_final_segment(seg(" ".join(["word"] * 30)))
        await pipe.wait_idle()

        assert not any(c["task"] == "generate_next_best_action" for c in mock.calls)
        assert store.suggestions == {}
        assert any(
            e.stage == "suggestions" and e.reason == "empty_fact_diff" for e in store.trigger_log
        )
        await pipe.close()

    async def test_model_error_is_surfaced_not_faked(self, monkeypatch):
        async def _boom(**kwargs):
            raise RuntimeError("openai down")

        monkeypatch.setattr(extraction.ai_client, "call_model", _boom)
        store = InMemoryStore()
        pipe = make_pipeline(store, _boom, FakeClock())
        await pipe.feed_final_segment(seg(" ".join(["word"] * 30)))
        await pipe.wait_idle()
        assert pipe.last_error is not None and "openai down" in pipe.last_error
        assert store.facts == {}
        await pipe.close()
