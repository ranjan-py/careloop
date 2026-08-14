"""Care-plan generation tests (spec §12/§13) — mocked provider (spec §2.3).

Covers: the single-model-call flow, unknown-fact-id dropping (spec §10),
deterministic permission-tier assignment (contracts v2), REAL retrieval-
produced evidence_refs (no network — BM25 over the local corpus), and the
dose-change language detector (spec §13 strategy note).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.ai.care_plan as cp_mod
from app.ai.care_plan import (
    CandidateAction,
    CarePlanDraft,
    ModelAction,
    assign_permission_tiers,
    attach_evidence,
    build_care_plan_input,
    find_dose_change_language,
    generate_care_plan,
    validate_fact_ids,
)
from app.ai.extraction import FactView
from app.ai.prompts import PromptRegistry
from app.ai.retrieval import get_index
from app.observability.tracing import EncounterTracer

CORPUS_PATH = Path(__file__).resolve().parents[2] / "data" / "evidence" / "evidence_corpus.json"


class _FallbackRegistry(PromptRegistry):
    def _client(self):
        return None


def miller_facts() -> list[FactView]:
    return [
        FactView(
            id="med-lisinopril", fact_type="medication_status", subject="lisinopril",
            value="active", source_type="ehr", source_class="synthea_ehr",
            verification_status="disputed",
        ),
        FactView(
            id="lab-potassium", fact_type="lab", subject="Potassium",
            value="4.2 mmol/L", source_type="ehr", source_class="synthea_ehr",
            verification_status="confirmed",
        ),
        FactView(
            id="fact-lis-stop", fact_type="medication_status", subject="lisinopril",
            value="stopped two weeks ago due to dizziness", source_type="patient_report",
            source_class="patient_report", verification_status="unverified",
            encounter_id="enc_t", conflicts_with="med-lisinopril",
        ),
        FactView(
            id="fact-kiosk-bp", fact_type="observation", subject="Blood pressure",
            value="150/95 mmHg", source_type="patient_report",
            source_class="patient_report", verification_status="unverified",
            method="pharmacy_kiosk", encounter_id="enc_t",
        ),
        FactView(
            id="fact-no-cuff", fact_type="care_gap", subject="Home BP monitor",
            value="patient does not own a home blood pressure cuff",
            source_type="patient_report", source_class="patient_report",
            verification_status="unverified", encounter_id="enc_t",
        ),
    ]


def model_action(category: str, title: str, *, facts: list[str] | None = None,
                 risk: str = "low", rationale: str | None = None) -> ModelAction:
    return ModelAction(
        category=category,  # type: ignore[arg-type]
        title=title,
        description=f"{title} as a small workflow step.",
        rationale=rationale or f"Motivated by the visit facts ({title.lower()}).",
        patient_facts_used=facts or [],
        risk_level=risk,  # type: ignore[arg-type]
    )


def standard_draft() -> CarePlanDraft:
    return CarePlanDraft(actions=[
        model_action(
            "medication", "Review antihypertensive therapy options",
            facts=["fact-lis-stop", "med-lisinopril", "bogus-id-123"], risk="medium",
            rationale="Patient reports stopping lisinopril due to dizziness while BP remains elevated.",
        ),
        model_action(
            "lab", "Order updated potassium and kidney function labs",
            facts=["lab-potassium", "fact-lis-stop"], risk="low",
            rationale="Potassium and kidney labs are months old and medication status changed.",
        ),
        model_action(
            "monitoring", "Arrange reliable blood pressure monitoring",
            facts=["fact-kiosk-bp", "fact-no-cuff"], risk="low",
            rationale="Only pharmacy kiosk readings available; patient has no home monitor.",
        ),
        model_action(
            "follow_up", "Schedule a two-week follow-up visit",
            facts=["fact-lis-stop"], risk="low",
            rationale="Re-assess once updated labs and reliable readings are available.",
        ),
        model_action(
            "other", "Generate patient-friendly visit instructions",
            facts=["fact-no-cuff"], risk="low",
            rationale="Patient needs plain-language instructions for the next steps.",
        ),
    ])


def patch_model(monkeypatch, drafts: list[CarePlanDraft]):
    calls: dict = {"n": 0, "kwargs": []}

    async def _mock(*, task, instructions, input_text, output_schema, model=None,
                    prompt_version=None, usage_sink=None):
        assert task == "generate_care_plan"
        assert output_schema is CarePlanDraft
        calls["kwargs"].append({
            "instructions": instructions, "input_text": input_text,
            "model": model, "prompt_version": prompt_version,
        })
        if usage_sink is not None:
            usage_sink.update({"model": "mock-model", "input_tokens": 100,
                               "output_tokens": 50, "total_tokens": 150})
        idx = min(calls["n"], len(drafts) - 1)
        calls["n"] += 1
        return drafts[idx]

    monkeypatch.setattr(cp_mod.ai_client, "call_model", _mock)
    return calls


async def run_generate(monkeypatch, drafts: list[CarePlanDraft] | None = None,
                       facts: list[FactView] | None = None):
    calls = patch_model(monkeypatch, drafts or [standard_draft()])
    actions = await generate_care_plan(
        "enc_t", "pt_miller", facts if facts is not None else miller_facts(),
        "Doctor: ...\nPatient: I stopped the lisinopril because it made me dizzy.",
        None,
        registry=_FallbackRegistry(),
        tracer=EncounterTracer(None),
        evidence_index=get_index(CORPUS_PATH),
    )
    return actions, calls


class TestGenerateCarePlan:
    async def test_single_model_call_produces_candidate_actions(self, monkeypatch):
        actions, calls = await run_generate(monkeypatch)
        assert calls["n"] == 1  # ONE model call (spec §13)
        assert len(actions) == 5
        assert all(isinstance(a, CandidateAction) for a in actions)
        assert {a.category for a in actions} == {"medication", "lab", "monitoring", "follow_up", "other"}

    async def test_prompt_input_carries_fact_inventory_and_transcript(self, monkeypatch):
        _, calls = await run_generate(monkeypatch)
        input_text = calls["kwargs"][0]["input_text"]
        assert "med-lisinopril" in input_text and "fact-kiosk-bp" in input_text
        assert "FINALIZED ENCOUNTER TRANSCRIPT" in input_text
        assert "I stopped the lisinopril" in input_text

    async def test_unknown_fact_ids_dropped(self, monkeypatch):
        actions, _ = await run_generate(monkeypatch)
        med = next(a for a in actions if a.category == "medication")
        assert "bogus-id-123" not in med.patient_facts_used
        assert med.patient_facts_used == ["fact-lis-stop", "med-lisinopril"]
        valid = {f.id for f in miller_facts()}
        for action in actions:
            assert set(action.patient_facts_used) <= valid

    async def test_evidence_refs_are_retrieval_produced(self, monkeypatch):
        actions, _ = await run_generate(monkeypatch)
        corpus_ids = {s.id for s in get_index(CORPUS_PATH).snippets}
        assert any(a.evidence_refs for a in actions)
        for action in actions:
            assert len(action.evidence_refs) <= 2
            assert set(action.evidence_refs) <= corpus_ids
        med = next(a for a in actions if a.category == "medication")
        assert "ev-006" in med.evidence_refs  # ACEi dizziness snippet
        monitoring = next(a for a in actions if a.category == "monitoring")
        assert set(monitoring.evidence_refs) & {"ev-021", "ev-022", "ev-023"}

    async def test_tiers_assigned_per_contract(self, monkeypatch):
        actions, _ = await run_generate(monkeypatch)
        med = next(a for a in actions if a.category == "medication")
        assert med.permission == "required_clinician_decision"
        auto = [a for a in actions if a.permission == "auto_demo"]
        assert len(auto) == 1 and auto[0].category == "other"  # patient instructions
        rest = [a for a in actions if a is not auto[0] and a is not med]
        assert all(a.permission == "clinician_review" for a in rest)

    async def test_versions_recorded_on_actions(self, monkeypatch):
        actions, _ = await run_generate(monkeypatch)
        assert all(a.prompt_version == "careloop_care_plan@fallback" for a in actions)
        assert all(a.model_version == "mock-model" for a in actions)

    async def test_empty_plan_raises_honest_error(self, monkeypatch):
        with pytest.raises(ValueError, match="honest failure"):
            await run_generate(monkeypatch, drafts=[CarePlanDraft(actions=[])])

    async def test_overlong_plan_truncated_to_max(self, monkeypatch):
        draft = standard_draft()
        draft.actions += [model_action("referral", "Refer to nephrology"),
                          model_action("other", "Extra action")]
        actions, _ = await run_generate(monkeypatch, drafts=[draft])
        assert len(actions) == 5

    async def test_usage_sink_filled(self, monkeypatch):
        patch_model(monkeypatch, [standard_draft()])
        sink: dict = {}
        await generate_care_plan(
            "enc_t", "pt_miller", miller_facts(), "transcript", None,
            usage_sink=sink, registry=_FallbackRegistry(), tracer=EncounterTracer(None),
            evidence_index=get_index(CORPUS_PATH),
        )
        assert sink["total_tokens"] == 150


class TestPermissionTierRules:
    def make(self, category: str, risk: str, title: str = "t") -> CandidateAction:
        return CandidateAction(category=category, title=title, description="d",
                               rationale="r", risk_level=risk)  # type: ignore[arg-type]

    def test_prefers_low_risk_other_action(self):
        actions = [self.make("lab", "low"), self.make("other", "low"), self.make("medication", "medium")]
        assign_permission_tiers(actions)
        assert [a.permission for a in actions] == [
            "clinician_review", "auto_demo", "required_clinician_decision"]

    def test_falls_back_to_any_low_risk_action(self):
        actions = [self.make("follow_up", "medium"), self.make("lab", "low")]
        assign_permission_tiers(actions)
        assert [a.permission for a in actions] == ["clinician_review", "auto_demo"]

    def test_falls_back_to_lowest_risk_when_no_low(self):
        actions = [self.make("lab", "high"), self.make("monitoring", "medium"),
                   self.make("medication", "medium")]
        assign_permission_tiers(actions)
        assert actions[1].permission == "auto_demo"
        assert actions[0].permission == "clinician_review"
        assert actions[2].permission == "required_clinician_decision"

    def test_medication_never_gets_auto_demo(self):
        actions = [self.make("medication", "low"), self.make("medication", "low")]
        assign_permission_tiers(actions)
        assert all(a.permission == "required_clinician_decision" for a in actions)
        assert not any(a.permission == "auto_demo" for a in actions)

    def test_exactly_one_auto_demo(self):
        actions = [self.make("other", "low"), self.make("lab", "low"),
                   self.make("follow_up", "low"), self.make("monitoring", "low")]
        assign_permission_tiers(actions)
        assert sum(a.permission == "auto_demo" for a in actions) == 1


class TestDeterministicHelpers:
    def test_validate_fact_ids(self):
        kept, dropped = validate_fact_ids(
            ["f1", "bogus", "f2", "f1"], valid_ids={"f1", "f2", "f3"})
        assert kept == ["f1", "f2"]
        assert dropped == ["bogus"]

    def test_build_input_empty_inventory(self):
        text = build_care_plan_input([], "hello")
        assert "(no facts on record)" in text

    def test_attach_evidence_off_topic_action_gets_empty_refs(self):
        action = CandidateAction(category="other", title="Discuss parking arrangements",
                                 description="d", rationale="Logistics only.", risk_level="low")
        attach_evidence([action], index=get_index(CORPUS_PATH))
        assert action.evidence_refs == []  # empty is allowed and honest

    def test_dose_change_language_detected(self):
        assert find_dose_change_language("Increase lisinopril to 20 mg daily") is not None
        assert find_dose_change_language("Start amlodipine 5 mg once daily") is not None
        assert find_dose_change_language("reduce the dose to 12.5 milligrams") is not None

    def test_process_language_not_flagged(self):
        assert find_dose_change_language(
            "Review antihypertensive therapy options given reported dizziness") is None
        assert find_dose_change_language(
            "Order updated potassium and kidney function labs") is None
        # Restating an existing regimen without a directive is not a dose change.
        assert find_dose_change_language("Patient takes metformin 1000 mg twice daily") is None
