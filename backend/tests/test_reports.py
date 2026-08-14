"""Report + classifier tests (spec §14/§16) — mocked provider (spec §2.3).

Covers: the consolidated audience-parameterized prompt family, the
deterministic leakage_check (planted rejected-action leaks and modified-
original leaks), the retry-once-then-raise contract, exclusion of rejected
actions and superseded originals from the prompt input, and the rejection-
category classifier (small OPENAI_EVAL_MODEL call; clinician stays
authoritative)."""

from __future__ import annotations

import pytest

import app.ai.classifier as clf_mod
import app.ai.reports as rep_mod
from app.ai.classifier import RejectionSuggestion, suggest_rejection_category
from app.ai.extraction import FactView
from app.ai.prompts import PromptRegistry
from app.ai.reports import (
    DecidedAction,
    RejectedAction,
    ReportLeakageError,
    ReportOutput,
    build_report_input,
    generate_reports,
    leakage_check,
)
from app.config import get_settings
from app.observability.tracing import EncounterTracer
from app.schemas.core import RejectionCategory


class _FallbackRegistry(PromptRegistry):
    def _client(self):
        return None


REJECTED_TITLE = "Begin twice-daily home blood pressure monitoring"
MODIFIED_ORIGINAL = "Schedule follow-up in three months"
MODIFIED_FINAL = "Schedule follow-up in two weeks"


def facts() -> list[FactView]:
    return [
        FactView(id="fact-lis-stop", fact_type="medication_status", subject="lisinopril",
                 value="stopped due to dizziness", source_type="patient_report",
                 source_class="patient_report", verification_status="unverified"),
        FactView(id="lab-potassium", fact_type="lab", subject="Potassium", value="4.2 mmol/L",
                 source_type="ehr", source_class="synthea_ehr", verification_status="confirmed"),
    ]


def decided() -> list[DecidedAction]:
    return [
        DecidedAction(category="lab", title="Order updated potassium and kidney labs",
                      description="Draw BMP with potassium and eGFR before the next visit.",
                      status="approved"),
        DecidedAction(category="follow_up", title=MODIFIED_FINAL,
                      description="Book a return visit in two weeks to review lab results.",
                      status="modified", original_title=MODIFIED_ORIGINAL,
                      original_description="Book a routine return visit in three months."),
    ]


def rejected() -> list[RejectedAction]:
    return [RejectedAction(title=REJECTED_TITLE, category="monitoring")]


CLEAN_BODIES = {
    "clinician": (
        "Patient-reported lisinopril discontinuation documented. Final plan: order "
        "updated potassium and kidney labs; schedule follow-up in two weeks."
    ),
    "patient": (
        "Please get your blood tests done this week, and come back to see us in two weeks."
    ),
}


def audience_of(instructions: str) -> str:
    return "patient" if "audience: patient" in instructions.lower() else "clinician"


def patch_reports_model(monkeypatch, bodies_by_call: dict[str, list[str]]):
    """bodies_by_call: audience -> list of report bodies returned per attempt."""
    calls: dict = {"n": 0, "per_audience": {"clinician": 0, "patient": 0}, "inputs": []}

    async def _mock(*, task, instructions, input_text, output_schema, model=None,
                    prompt_version=None, usage_sink=None, reasoning_effort=None):
        assert output_schema is ReportOutput
        audience = audience_of(instructions)
        attempt = calls["per_audience"][audience]
        calls["per_audience"][audience] += 1
        calls["n"] += 1
        calls["inputs"].append(input_text)
        if usage_sink is not None:
            usage_sink.update({"model": "mock-model", "input_tokens": 200,
                               "output_tokens": 80, "total_tokens": 280})
        bodies = bodies_by_call[audience]
        return ReportOutput(title=f"{audience} report",
                            body=bodies[min(attempt, len(bodies) - 1)])

    monkeypatch.setattr(rep_mod.ai_client, "call_model", _mock)
    return calls


async def run_reports(monkeypatch, bodies_by_call: dict[str, list[str]]):
    calls = patch_reports_model(monkeypatch, bodies_by_call)
    bundle = await generate_reports(
        "enc_t", "pt_miller", facts(), decided(), rejected(), None,
        registry=_FallbackRegistry(), tracer=EncounterTracer(None),
    )
    return bundle, calls


class TestLeakageCheck:
    def test_clean_report_passes(self):
        assert leakage_check(CLEAN_BODIES["clinician"], [REJECTED_TITLE],
                             [(MODIFIED_ORIGINAL, MODIFIED_FINAL)]) == []

    def test_planted_rejected_action_caught(self):
        text = ("Plan: order labs. Also begin twice-daily home blood pressure "
                "monitoring starting tomorrow.")
        violations = leakage_check(text, [REJECTED_TITLE], [])
        assert len(violations) == 1 and "rejected action" in violations[0]

    def test_rejected_match_is_case_and_punctuation_insensitive(self):
        text = "We recommend: BEGIN TWICE-DAILY HOME BLOOD-PRESSURE MONITORING."
        assert leakage_check(text, [REJECTED_TITLE], []) != []

    def test_modified_original_leak_caught(self):
        text = f"Next steps: {MODIFIED_ORIGINAL} and order labs."
        violations = leakage_check(text, [], [(MODIFIED_ORIGINAL, MODIFIED_FINAL)])
        assert len(violations) == 1 and "superseded original" in violations[0]

    def test_final_wording_is_not_a_violation(self):
        text = f"Next steps: {MODIFIED_FINAL}."
        assert leakage_check(text, [], [(MODIFIED_ORIGINAL, MODIFIED_FINAL)]) == []

    def test_original_contained_in_final_is_not_a_violation(self):
        # Clinician EXTENDED the wording — the original inside the final is expected.
        original, final = "Order labs", "Order labs and review at follow-up"
        assert leakage_check(f"Plan: {final}.", [], [(original, final)]) == []

    def test_empty_inputs_never_violate(self):
        assert leakage_check("anything", [], []) == []
        assert leakage_check("anything", [""], [("", "x")]) == []


class TestReportInput:
    def test_modified_actions_appear_only_in_final_form(self):
        text = build_report_input(facts(), decided(), rejected())
        assert MODIFIED_FINAL in text
        assert MODIFIED_ORIGINAL not in text  # superseded original never shown to the model
        assert "three months" not in text

    def test_rejected_actions_listed_only_as_exclusions(self):
        text = build_report_input(facts(), decided(), rejected())
        assert REJECTED_TITLE in text
        excluded_section = text.split("EXCLUDED ACTIONS")[1]
        assert REJECTED_TITLE in excluded_section
        plan_section = text.split("FINAL PLAN")[1].split("EXCLUDED ACTIONS")[0]
        assert REJECTED_TITLE not in plan_section

    def test_fact_inventory_present(self):
        text = build_report_input(facts(), decided(), rejected())
        assert "fact-lis-stop" in text and "lab-potassium" in text


class TestGenerateReports:
    async def test_both_audiences_generated_via_one_prompt_family(self, monkeypatch):
        bundle, calls = await run_reports(
            monkeypatch, {k: [v] for k, v in CLEAN_BODIES.items()})
        assert calls["n"] == 2  # one call per audience, same prompt family
        assert calls["per_audience"] == {"clinician": 1, "patient": 1}
        assert "two weeks" in bundle.clinician_summary
        assert "two weeks" in bundle.patient_instructions
        assert bundle.clinician.audience == "clinician"
        assert bundle.patient.audience == "patient"
        assert not bundle.clinician.retried and not bundle.patient.retried
        assert bundle.clinician.prompt_version == "careloop_report@fallback"

    async def test_leaking_report_retried_once_then_succeeds(self, monkeypatch):
        leaking = f"Do labs. Also: {REJECTED_TITLE}."
        bundle, calls = await run_reports(monkeypatch, {
            "clinician": [CLEAN_BODIES["clinician"]],
            "patient": [leaking, CLEAN_BODIES["patient"]],
        })
        assert calls["per_audience"] == {"clinician": 1, "patient": 2}
        assert bundle.patient.retried is True
        assert leakage_check(bundle.patient_instructions, [REJECTED_TITLE], []) == []

    async def test_persistent_leak_raises_after_retry(self, monkeypatch):
        leaking = f"Also: {REJECTED_TITLE}."
        with pytest.raises(ReportLeakageError) as err:
            await run_reports(monkeypatch, {
                "clinician": [CLEAN_BODIES["clinician"]],
                "patient": [leaking, leaking],
            })
        assert err.value.audience == "patient"
        assert any("rejected action" in v for v in err.value.violations)

    async def test_modified_original_leak_also_retried(self, monkeypatch):
        leaking = f"Plan: {MODIFIED_ORIGINAL}."
        bundle, calls = await run_reports(monkeypatch, {
            "clinician": [leaking, CLEAN_BODIES["clinician"]],
            "patient": [CLEAN_BODIES["patient"]],
        })
        assert calls["per_audience"]["clinician"] == 2
        assert bundle.clinician.retried is True

    async def test_usage_sink_aggregates_all_calls(self, monkeypatch):
        patch_reports_model(monkeypatch, {k: [v] for k, v in CLEAN_BODIES.items()})
        sink: dict = {}
        await generate_reports(
            "enc_t", "pt_miller", facts(), decided(), rejected(), None,
            usage_sink=sink, registry=_FallbackRegistry(), tracer=EncounterTracer(None),
        )
        assert sink["total_tokens"] == 560  # two calls x 280


class TestRejectionClassifier:
    async def test_no_home_monitor_maps_to_patient_limitation(self, monkeypatch):
        seen: dict = {}

        async def _mock(*, task, instructions, input_text, output_schema, model=None,
                        prompt_version=None, usage_sink=None, reasoning_effort=None):
            assert task == "classify_override"
            assert output_schema is RejectionSuggestion
            seen.update({"model": model, "input": input_text, "instructions": instructions})
            return RejectionSuggestion(category=RejectionCategory.PATIENT_LIMITATION)

        monkeypatch.setattr(clf_mod.ai_client, "call_model", _mock)
        category = await suggest_rejection_category(
            "Patient checks BP at a pharmacy kiosk occasionally; no home monitor "
            "for a twice-daily protocol.",
            action_title="Home BP twice daily",
            registry=_FallbackRegistry(), tracer=EncounterTracer(None),
        )
        assert category is RejectionCategory.PATIENT_LIMITATION
        # Small classification call runs on the cheap eval model (spec §10/§14).
        assert seen["model"] == get_settings().openai_eval_model
        assert "no home monitor" in seen["input"]
        assert "Home BP twice daily" in seen["input"]

    async def test_empty_reason_rejected_without_model_call(self, monkeypatch):
        async def _mock(**kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("model must not be called for empty input")

        monkeypatch.setattr(clf_mod.ai_client, "call_model", _mock)
        with pytest.raises(ValueError):
            await suggest_rejection_category("   ", registry=_FallbackRegistry(),
                                             tracer=EncounterTracer(None))
