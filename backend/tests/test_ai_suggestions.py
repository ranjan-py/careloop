"""Suggestion engine tests (spec §8C/§9) — dedup scoped PER ENCOUNTER,
cooldown, max-2-active supersede, prompt-version recording. Mocked provider."""

from __future__ import annotations

import pytest

import app.ai.suggestions as sug_mod
from app.ai.prompts import PromptRegistry
from app.ai.suggestions import (
    ActiveSuggestionView,
    NextBestSuggestions,
    SuggestionCandidate,
    SuggestionEngine,
    normalize_dedup_key,
)
from app.observability.tracing import EncounterTracer


def candidate(key: str, text: str | None = None, kind: str = "question") -> SuggestionCandidate:
    return SuggestionCandidate(
        kind=kind,  # type: ignore[arg-type]
        text=text or f"Ask about {key}.",
        rationale=f"Because of {key}.",
        dedup_key=key,
    )


class _FallbackRegistry(PromptRegistry):
    def _client(self):
        return None


def make_engine(encounter_id: str = "enc_a", cooldown: float = 15.0, max_active: int = 2) -> SuggestionEngine:
    return SuggestionEngine(
        encounter_id=encounter_id,
        registry=_FallbackRegistry(),
        tracer=EncounterTracer(None),
        cooldown_seconds=cooldown,
        max_active=max_active,
    )


def patch_model(monkeypatch, suggestions_by_call: list[list[SuggestionCandidate]]):
    calls = {"n": 0, "inputs": []}

    async def _mock(*, task, instructions, input_text, output_schema, model=None,
                    prompt_version=None, usage_sink=None, reasoning_effort=None):
        assert task == "generate_next_best_action"
        calls["inputs"].append(input_text)
        idx = min(calls["n"], len(suggestions_by_call) - 1)
        calls["n"] += 1
        return NextBestSuggestions(suggestions=suggestions_by_call[idx])

    monkeypatch.setattr(sug_mod.ai_client, "call_model", _mock)
    return calls


async def propose(engine, *, existing_keys=frozenset(), active=(), now=1000.0):
    return await engine.propose(
        inventory_lines=["f1 | lab | Potassium: 4.1 (ehr, confirmed)"],
        new_fact_lines=["NEW f2 | medication_status | lisinopril: stopped"],
        transcript_window="Patient: I stopped the lisinopril.",
        existing_keys=set(existing_keys),
        active=list(active),
        now=now,
    )


class TestDedupScoping:
    async def test_same_key_blocked_within_encounter(self, monkeypatch):
        patch_model(monkeypatch, [[candidate("dizziness_timing")]])
        engine = make_engine("enc_a")
        outcome = await propose(engine, existing_keys={"dizziness_timing"})
        assert outcome.new_rows == []
        assert outcome.skipped == [("dizziness_timing", "dedup_key already used this encounter")]

    async def test_same_key_allowed_in_different_encounter(self, monkeypatch):
        """Rehearsal encounters must never suppress the live run (spec §8C/§30):
        dedup keys are read from the store PER ENCOUNTER, and engine state is
        per-encounter too."""
        patch_model(monkeypatch, [[candidate("dizziness_timing")]])
        # Encounter B knows nothing of encounter A's keys — its store returns
        # only ITS OWN dedup keys (here: none).
        engine_b = make_engine("enc_b")
        outcome = await propose(engine_b, existing_keys=set())
        assert len(outcome.new_rows) == 1
        assert outcome.new_rows[0].encounter_id == "enc_b"
        assert outcome.new_rows[0].dedup_key == "dizziness_timing"

    async def test_keys_normalized_before_comparison(self, monkeypatch):
        patch_model(monkeypatch, [[candidate("  Dizziness  Timing!! ")]])
        engine = make_engine()
        outcome = await propose(engine, existing_keys={"dizziness_timing"})
        assert outcome.new_rows == []

    def test_normalize_dedup_key(self):
        assert normalize_dedup_key("  Dizziness--Timing (Lisinopril) ") == "dizziness_timing_lisinopril"
        assert normalize_dedup_key("!!!") == "unkeyed"

    async def test_duplicate_keys_within_one_run_deduped(self, monkeypatch):
        patch_model(monkeypatch, [[candidate("potassium_recency"), candidate("potassium_recency")]])
        engine = make_engine()
        outcome = await propose(engine)
        assert len(outcome.new_rows) == 1


class TestCooldown:
    async def test_second_emission_within_cooldown_skipped_without_model_call(self, monkeypatch):
        calls = patch_model(monkeypatch, [[candidate("k1")], [candidate("k2")]])
        engine = make_engine(cooldown=15.0)
        first = await propose(engine, now=1000.0)
        assert len(first.new_rows) == 1
        second = await propose(engine, existing_keys={"k1"}, now=1010.0)  # 10s later
        assert second.new_rows == []
        assert second.skipped and second.skipped[0][1].startswith("cooldown")
        assert calls["n"] == 1  # cooldown gate saves the model call entirely

    async def test_emission_allowed_after_cooldown(self, monkeypatch):
        patch_model(monkeypatch, [[candidate("k1")], [candidate("k2")]])
        engine = make_engine(cooldown=15.0)
        await propose(engine, now=1000.0)
        third = await propose(engine, existing_keys={"k1"}, now=1016.0)  # 16s later
        assert len(third.new_rows) == 1
        assert third.new_rows[0].dedup_key == "k2"


class TestMaxActive:
    async def test_third_active_supersedes_oldest(self, monkeypatch):
        patch_model(monkeypatch, [[candidate("k3")]])
        engine = make_engine(max_active=2)
        active = [
            ActiveSuggestionView(id="sug_old", dedup_key="k1", created_order=1.0, text="Old"),
            ActiveSuggestionView(id="sug_new", dedup_key="k2", created_order=2.0, text="New"),
        ]
        outcome = await propose(engine, existing_keys={"k1", "k2"}, active=active)
        assert len(outcome.new_rows) == 1
        assert outcome.supersede_ids == ["sug_old"]
        remove = [e for e in outcome.envelopes if e.type == "suggestion.remove"]
        add = [e for e in outcome.envelopes if e.type == "suggestion.active"]
        assert [e.suggestion_id for e in remove] == ["sug_old"]
        assert len(add) == 1 and add[0].suggestion.status == "active"

    async def test_under_cap_supersedes_nothing(self, monkeypatch):
        patch_model(monkeypatch, [[candidate("k2")]])
        engine = make_engine(max_active=2)
        active = [ActiveSuggestionView(id="sug_a", dedup_key="k1", created_order=1.0, text="A")]
        outcome = await propose(engine, existing_keys={"k1"}, active=active)
        assert outcome.supersede_ids == []
        assert len(outcome.new_rows) == 1

    async def test_per_run_cap_limits_candidates(self, monkeypatch):
        patch_model(monkeypatch, [[candidate(f"k{i}") for i in range(5)]])
        engine = make_engine()
        outcome = await propose(engine)
        assert len(outcome.new_rows) == engine.max_suggestions_per_run == 2


class TestPromptVersionRecording:
    async def test_prompt_version_recorded_on_outcome(self, monkeypatch):
        patch_model(monkeypatch, [[candidate("k1")]])
        engine = make_engine()
        outcome = await propose(engine)
        assert outcome.prompt_version == "careloop_next_best_question@fallback"

    async def test_active_texts_and_used_keys_fed_to_model(self, monkeypatch):
        calls = patch_model(monkeypatch, [[candidate("k9")]])
        engine = make_engine()
        active = [ActiveSuggestionView(id="s1", dedup_key="k1", created_order=1.0,
                                       text="Confirm home monitor availability.")]
        await propose(engine, existing_keys={"k1"}, active=active)
        sent = calls["inputs"][0]
        assert "Confirm home monitor availability." in sent
        assert "k1" in sent
