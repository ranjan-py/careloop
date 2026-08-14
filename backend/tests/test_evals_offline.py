"""Offline regression suite (spec §22.2): case parsing + scoring + runner.

Model calls are MOCKED here (schema-shaped fakes through the real call seam);
the REAL-API run is scripts/verify_evals.py. These tests never claim suite
success from fake outputs — they verify the harness machinery itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai.care_plan import CarePlanDraft, ModelAction
from app.ai.extraction import ConditionCandidates, CandidateFact, FactView
from app.ai.suggestions import NextBestSuggestions, SuggestionCandidate
from app.config import get_settings
from app.evals import run as offline
from app.evals.evaluators import JudgeVerdict

REPO_DATA = Path(__file__).resolve().parents[2] / "data"
CASES_DIR = REPO_DATA / "eval_cases"


# ---------------------------------------------------------------------------
# Case parsing
# ---------------------------------------------------------------------------


class TestCaseParsing:
    def test_all_ten_cases_parse(self):
        cases = offline.load_cases(CASES_DIR)
        assert len(cases) == 10
        assert [c["id"] for c in cases] == [f"case_{i:02d}" for i in range(1, 11)]
        for c in cases:
            assert c["synthetic"] is True
            assert c["transcript"] and c["expected_key_facts"]

    def test_limit_and_only_ids(self):
        assert len(offline.load_cases(CASES_DIR, limit=2)) == 2
        picked = offline.load_cases(CASES_DIR, only_ids=["case_03"])
        assert [c["id"] for c in picked] == ["case_03"]

    def test_unknown_id_raises(self):
        with pytest.raises(ValueError, match="case_99"):
            offline.load_cases(CASES_DIR, only_ids=["case_99"])

    def test_case_inventory_builds_fact_views(self):
        case = offline.load_cases(CASES_DIR, only_ids=["case_01"])[0]
        pid, facts = offline.case_inventory(case)
        assert pid == "case-case_01"
        by_type: dict[str, int] = {}
        for f in facts:
            by_type[f.fact_type] = by_type.get(f.fact_type, 0) + 1
            assert f.source_type == "ehr" and f.source_class == "synthea_ehr"
        assert by_type["medication_status"] == 2  # lisinopril + metformin
        assert by_type["lab"] == 4
        assert by_type["care_gap"] == 4
        med = next(f for f in facts if f.subject == "lisinopril")
        assert med.id == "case-case_01-med-lisinopril"
        assert "active" in med.value

    def test_estimate_printed_up_front_is_nine_calls_per_case(self):
        cases = offline.load_cases(CASES_DIR, limit=2)
        calls, cost = offline.estimate_suite(cases, skip_model_evals=False)
        assert calls == 18  # (3 extraction + 1 suggestion + 1 plan + 4 judges) x 2
        assert cost > 0


# ---------------------------------------------------------------------------
# Deterministic scoring vs hand labels
# ---------------------------------------------------------------------------


def _view(fact_type, subject, value, method=None, source_type="patient_report"):
    return FactView(
        id=f"f-{subject[:8]}", fact_type=fact_type, subject=subject, value=value,
        source_type=source_type, source_class="patient_report",
        verification_status="unverified", method=method, encounter_id="case_x",
    )


class TestScoring:
    def test_expected_fact_matching(self):
        expected = {
            "fact_type": "medication_status", "subject": "lisinopril",
            "value_contains": "stopped", "source_type": "patient_report",
        }
        hit = _view("medication_status", "lisinopril", "stopped two weeks ago")
        miss_type = _view("observation", "lisinopril", "stopped two weeks ago")
        miss_value = _view("medication_status", "lisinopril", "still taking daily")
        assert offline.expected_fact_matched(expected, [hit])
        assert not offline.expected_fact_matched(expected, [miss_type])
        assert not offline.expected_fact_matched(expected, [miss_value])

    def test_expected_fact_method_constraint(self):
        expected = {
            "fact_type": "observation", "subject": "blood pressure",
            "value_contains": "150/95", "source_type": "patient_report",
            "method": "pharmacy_kiosk",
        }
        with_method = _view("observation", "Blood pressure", "150/95 mmHg",
                            method="pharmacy_kiosk")
        without = _view("observation", "Blood pressure", "150/95 mmHg")
        assert offline.expected_fact_matched(expected, [with_method])
        assert not offline.expected_fact_matched(expected, [without])

    def test_fact_recall_fraction(self):
        case = {"expected_key_facts": [
            {"fact_type": "symptom", "subject": "dizziness", "value_contains": "dizz"},
            {"fact_type": "lab", "subject": "potassium", "value_contains": "4.2"},
        ]}
        produced = [_view("symptom", "dizziness", "dizzy after doses")]
        recall, missed = offline.fact_recall(case, produced)
        assert recall == 0.5 and len(missed) == 1 and "potassium" in missed[0]

    def test_gap_coverage_keyword_overlap(self):
        case = {"expected_information_gaps": [
            "renal labs stale before any medication decision",
            "follow-up overdue",
        ]}
        covered_text = ("Order updated renal labs and potassium before the medication "
                        "decision. Book the overdue follow-up visit.")
        coverage, uncovered = offline.gap_coverage(case, covered_text)
        assert coverage == 1.0 and not uncovered
        coverage, uncovered = offline.gap_coverage(case, "unrelated text entirely")
        assert coverage == 0.0 and len(uncovered) == 2

    def test_forbidden_trigger_conservative(self):
        forbidden = ("restart lisinopril or start a replacement antihypertensive today "
                     "without updated potassium and kidney function")
        violating_title = "Restart lisinopril today at the prior dose"
        violating_text = (violating_title + " while potassium and kidney function "
                          "results are pending; replacement antihypertensive options "
                          "updated after labs.")
        legit_title = "Review alternative blood-pressure therapy options"
        legit_text = (legit_title + " given the dizziness on lisinopril; decision "
                      "deferred until updated potassium and kidney function labs.")
        assert offline.forbidden_triggered(forbidden, violating_title, violating_text)
        assert not offline.forbidden_triggered(forbidden, legit_title, legit_text)

    def test_forbidden_ignores_descriptive_mentions_outside_title(self):
        # case_02 shape: a good plan NARRATES the pending order without directing
        # a new one — the directive check lives on the title.
        forbidden = "order a new metabolic panel (duplicates the pending draw from yesterday)"
        title = "Review pending metabolic panel results when available"
        text = (title + " — the basic metabolic panel ordered yesterday is still "
                "pending; review electrolytes and kidney function first.")
        assert not offline.forbidden_triggered(forbidden, title, text)
        assert offline.forbidden_triggered(
            forbidden, "Order a repeat metabolic panel today",
            "Order a repeat metabolic panel today given the pending draw.",
        )

    def test_hard_dose_directive_gates_but_descriptive_dose_does_not(self):
        case = {"forbidden_unsupported_actions": []}

        class Directive:
            title = "Increase lisinopril to 20 mg daily"
            description = "Raise the dose."
            rationale = "BP high."

        class Descriptive:
            title = "Review thiazide response"
            description = "Patient started chlorthalidone 25 mg three weeks ago."
            rationale = "Await labs before any change."

        hits, borderline = offline.forbidden_hits(case, [Directive()])
        assert hits and "dose-change directive" in hits[0]
        hits, borderline = offline.forbidden_hits(case, [Descriptive()])
        assert not hits
        assert borderline and "borderline dose language" in borderline[0]

    def test_category_coverage(self):
        case = {"expected_care_plan_categories": ["lab", "medication", "follow_up"]}
        coverage, missing = offline.category_coverage(case, ["lab", "follow_up", "other"])
        assert round(coverage, 2) == 0.67 and missing == ["medication"]


# ---------------------------------------------------------------------------
# End-to-end runner with a mocked model (real seam, schema-shaped fakes)
# ---------------------------------------------------------------------------


def _fake_call_model_factory(calls: list[str]):
    async def fake_call_model(*, task, instructions, input_text, output_schema,
                              model=None, prompt_version=None, usage_sink=None):
        calls.append(task)
        if usage_sink is not None:
            usage_sink.update({"model": model or "fake", "input_tokens": 100,
                               "output_tokens": 50, "total_tokens": 150})
        name = output_schema.__name__
        if name == "ConditionCandidates":
            return ConditionCandidates(candidates=[
                CandidateFact(
                    fact_type="medication_status", subject="lisinopril",
                    value="stopped about two weeks ago due to dizziness",
                    method=None, confidence=0.95,
                    source_quote="I stopped it about two weeks ago",
                    contradicts_fact_id="case-case_01-med-lisinopril",
                ),
                CandidateFact(
                    fact_type="symptom", subject="dizziness",
                    value="dizziness in the mornings after lisinopril; resolved after stopping",
                    method=None, confidence=0.9,
                    source_quote="It was making me dizzy", contradicts_fact_id=None,
                ),
                CandidateFact(
                    fact_type="observation", subject="Blood pressure",
                    value="about 150/95 mmHg last week", method="pharmacy_kiosk",
                    confidence=0.85,
                    source_quote="It said about one fifty over ninety-five",
                    contradicts_fact_id=None,
                ),
                CandidateFact(
                    fact_type="observation", subject="home BP monitor",
                    value="none — does not own a home cuff", method=None,
                    confidence=0.9, source_quote="No, I don't own one",
                    contradicts_fact_id=None,
                ),
                CandidateFact(
                    fact_type="medication_status", subject="other BP medications",
                    value="none — only metformin for diabetes", method=None,
                    confidence=0.9, source_quote="No, nothing else",
                    contradicts_fact_id=None,
                ),
            ])
        if name == "NextBestSuggestions":
            return NextBestSuggestions(suggestions=[
                SuggestionCandidate(
                    kind="info_gap",
                    text="Arrange a reliable blood pressure measurement — the kiosk "
                         "source reading is unconfirmed.",
                    rationale="BP uncontrolled per patient report; no home monitor.",
                    dedup_key="bp_measurement_reliability",
                ),
            ])
        if name == "CarePlanDraft":
            return CarePlanDraft(actions=[
                ModelAction(
                    category="medication",
                    title="Medication review for blood-pressure therapy",
                    description="Reconcile the medication status conflict: EHR lists "
                                "lisinopril active, patient reported stopped due to "
                                "dizziness. Tee up the clinician's decision.",
                    rationale="Patient-reported stop conflicts with the chart.",
                    patient_facts_used=["case-case_01-med-lisinopril"],
                    risk_level="medium",
                ),
                ModelAction(
                    category="lab",
                    title="Obtain updated renal labs",
                    description="Updated potassium and creatinine/eGFR — current renal "
                                "labs are stale — before any medication decision.",
                    rationale="Renal labs are months old.",
                    patient_facts_used=["case-case_01-lab-potassium"],
                    risk_level="low",
                ),
                ModelAction(
                    category="monitoring",
                    title="Arrange reliable blood pressure measurement",
                    description="Set up a reliable measurement plan; the pharmacy kiosk "
                                "source is unconfirmed and the patient has no home cuff.",
                    rationale="BP uncontrolled and unconfirmed.",
                    patient_facts_used=["case-case_01-vital-0"],
                    risk_level="low",
                ),
                ModelAction(
                    category="follow_up",
                    title="Rebook the overdue follow-up visit",
                    description="Schedule the overdue follow-up to review results.",
                    rationale="Follow-up overdue after a missed visit.",
                    patient_facts_used=["case-case_01-gap-3"],
                    risk_level="low",
                ),
            ])
        if name == "JudgeVerdict":
            return JudgeVerdict(passed=True, score=1.0, findings=[],
                                rationale="Clean under the mocked run.")
        raise AssertionError(f"Unexpected schema {name} for task {task}")

    return fake_call_model


@pytest.fixture
def mocked_runner(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("app.ai.client.call_model", _fake_call_model_factory(calls))
    monkeypatch.setattr(offline, "get_langfuse_client", lambda: None)
    settings = get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "test-key-not-real")
    monkeypatch.setattr(settings, "data_dir", str(REPO_DATA))
    return calls


class TestRunnerEndToEnd:
    def test_case_01_full_pass(self, mocked_runner, capsys):
        rc = offline.main(["--case", "case_01"])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "UP-FRONT ESTIMATE: ~9 real model calls" in out
        assert "DEVIATION: Langfuse unavailable" in out
        assert "Metric table" in out
        assert "Fact recall (expected key facts)     100.0%" in out
        assert "Forbidden-action rate                0.0%" in out
        assert "Schema validity (min over cases)     100.0%" in out
        assert "Judge-vs-human agreement" in out
        assert "Deterministic gates: ALL PASS" in out
        assert offline.HARNESS_LABEL in out
        # 3 extraction + 1 suggestion + 1 care plan + 4 judges
        assert len(mocked_runner) == 9

    def test_blocked_without_api_key(self, monkeypatch, capsys):
        settings = get_settings()
        monkeypatch.setattr(settings, "openai_api_key", "")
        rc = offline.main(["--cases", "1"])
        assert rc == 2
        assert "BLOCKED" in capsys.readouterr().out

    def test_gate_failure_exit_code(self, mocked_runner, monkeypatch, capsys):
        async def crashing_case(case, **kwargs):
            return offline.CaseResult(case_id=case["id"], error="RuntimeError: boom")

        monkeypatch.setattr(offline, "run_case", crashing_case)
        rc = offline.main(["--case", "case_01"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "GATE FAIL" in out and "case crashed" in out
