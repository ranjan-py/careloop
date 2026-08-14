"""Deterministic online evaluators (spec §22.1) against fixture rows.

Fixture rows are unpersisted ORM instances — the evaluators are pure
functions over an EncounterSnapshot, so no database is required. Planted
violations (leakage, tool-map violation, permission violation, fidelity
break) MUST be caught; the clean encounter MUST pass every gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import models as m
from app.evals import evaluators
from app.evals.evaluators import (
    EncounterSnapshot,
    SCORE_LABEL,
    evaluate_latency,
    evaluate_modified_action_fidelity,
    evaluate_permission_behavior,
    evaluate_rejected_action_leakage,
    evaluate_schema_validity,
    evaluate_tool_selection_validity,
    estimate_encounter_cost,
    gates_passed,
    latency_metrics,
    run_deterministic_evaluators,
)

T0 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def _sec(n: float) -> datetime:
    return T0 + timedelta(seconds=n)


def make_snapshot(**overrides) -> EncounterSnapshot:
    """A clean, fully-decided encounter: 1 approved lab, 1 modified follow-up,
    1 rejected monitoring, 1 auto_demo other; reports render the FINAL
    follow-up wording and never mention the rejected monitoring action."""
    encounter = m.Encounter(
        id="enc_fix", patient_id="john-miller", status="finalized",
        mode="replay", trace_id="a" * 32, started_at=_sec(0), ended_at=_sec(120),
    )
    chart_facts = [
        m.Fact(
            id="med-lisinopril", patient_id="john-miller", fact_type="medication_status",
            subject="lisinopril", value="active", source_type="ehr",
            source_class="synthea_ehr", reported_at=_sec(0), ingested_at=_sec(0),
            confidence=1.0, verification_status="disputed",
        )
    ]
    facts = [
        m.Fact(
            id="fact_bp", patient_id="john-miller", encounter_id="enc_fix",
            fact_type="observation", subject="Blood pressure", value="150/95 mmHg",
            source_type="patient_report", source_class="patient_report",
            method="pharmacy_kiosk", reported_at=_sec(40), ingested_at=_sec(40),
            confidence=0.9, verification_status="unverified",
        ),
        m.Fact(
            id="fact_lis", patient_id="john-miller", encounter_id="enc_fix",
            fact_type="medication_status", subject="lisinopril", value="stopped",
            source_type="patient_report", source_class="patient_report",
            reported_at=_sec(70), ingested_at=_sec(70), confidence=0.95,
            verification_status="unverified", conflicts_with="med-lisinopril",
        ),
    ]
    segments = [
        m.TranscriptSegment(
            id=f"seg_{i}", encounter_id="enc_fix", speaker="patient",
            text="I stopped the lisinopril two weeks ago", ts=float(i * 10),
            is_final=True, created_at=_sec(5 + i * 10),
        )
        for i in range(3)
    ]
    suggestions = [
        m.Suggestion(
            id="sug_1", encounter_id="enc_fix", kind="question",
            text="Ask when the dizziness occurs relative to the lisinopril dose.",
            rationale="Patient stopped lisinopril due to dizziness.",
            status="active", dedup_key="dizziness_timing", created_at=_sec(43),
        )
    ]
    care_plan = m.CarePlan(
        id="cp_fix", encounter_id="enc_fix", patient_id="john-miller",
        status="finalized",
        clinician_summary=(
            "Visit summary. Plan: obtain updated potassium and kidney function labs. "
            "Schedule follow-up visit in ten days to review results."
        ),
        patient_instructions=(
            "Please get your blood tests done this week. Come back to see us in ten days."
        ),
    )
    actions = [
        m.CarePlanAction(
            id="act_lab", care_plan_id="cp_fix", category="lab",
            title="Order updated potassium and kidney function labs",
            description="Obtain BMP with potassium and creatinine/eGFR.",
            rationale="Renal labs are stale.", patient_facts_used=["fact_bp"],
            evidence_refs=[], risk_level="low", permission="clinician_review",
            status="executed", created_at=_sec(135),
        ),
        m.CarePlanAction(
            id="act_follow", care_plan_id="cp_fix", category="follow_up",
            title="Schedule follow-up visit in ten days",
            description="Book a return visit in ten days to review labs.",
            rationale="Follow-up overdue.", patient_facts_used=["fact_lis"],
            evidence_refs=[], risk_level="low", permission="clinician_review",
            status="executed", created_at=_sec(135),
        ),
        m.CarePlanAction(
            id="act_monitor", care_plan_id="cp_fix", category="monitoring",
            title="Begin twice-daily home blood pressure monitoring",
            description="Record BP twice a day with a home cuff.",
            rationale="BP uncontrolled.", patient_facts_used=["fact_bp"],
            evidence_refs=[], risk_level="low", permission="clinician_review",
            status="rejected", created_at=_sec(135),
        ),
        m.CarePlanAction(
            id="act_other", care_plan_id="cp_fix", category="other",
            title="Generate patient-friendly instructions",
            description="Provide plain-language instructions.",
            rationale="Support adherence.", patient_facts_used=[],
            evidence_refs=[], risk_level="low", permission="auto_demo",
            status="executed", created_at=_sec(135),
        ),
    ]
    decisions = {
        "act_lab": [
            m.Decision(
                id="dec_lab", action_id="act_lab", clinician_id="clin-1",
                decision_type="approve", original_title=actions[0].title,
                original_description=actions[0].description, decided_at=_sec(140),
            )
        ],
        "act_follow": [
            m.Decision(
                id="dec_follow", action_id="act_follow", clinician_id="clin-1",
                decision_type="modify",
                original_title="Schedule follow-up visit in one month",
                original_description="Book a return visit in one month.",
                final_title="Schedule follow-up visit in ten days",
                final_description="Book a return visit in ten days to review labs.",
                reason="Tighter interval.", decided_at=_sec(141),
            )
        ],
        "act_monitor": [
            m.Decision(
                id="dec_monitor", action_id="act_monitor", clinician_id="clin-1",
                decision_type="reject", original_title=actions[2].title,
                original_description=actions[2].description,
                rejection_category="patient_limitation",
                remarks="No home monitor.", decided_at=_sec(142),
            )
        ],
    }
    executions = [
        m.ToolExecution(
            id="tex_lab", encounter_id="enc_fix", action_id="act_lab",
            tool_name="create_demo_lab_order", status="executed",
            permission_tier="clinician_review", result={}, executed_at=_sec(150),
        ),
        m.ToolExecution(
            id="tex_follow", encounter_id="enc_fix", action_id="act_follow",
            tool_name="schedule_demo_followup", status="executed",
            permission_tier="clinician_review", result={}, executed_at=_sec(150),
        ),
        m.ToolExecution(
            id="tex_other", encounter_id="enc_fix", action_id="act_other",
            tool_name="generate_patient_instructions", status="executed",
            permission_tier="auto_demo", result={}, executed_at=_sec(150),
        ),
    ]
    trigger_logs = [
        m.TriggerLog(
            id="trg_1", encounter_id="enc_fix", stage="extraction", decision="fired",
            reason="word_threshold", buffered_words=30, buffered_seconds=8.0,
            detail={}, created_at=_sec(35),
        ),
        m.TriggerLog(
            id="trg_2", encounter_id="enc_fix", stage="extraction", decision="skipped",
            reason="below_threshold", buffered_words=10, buffered_seconds=3.0,
            detail={}, created_at=_sec(55),
        ),
        m.TriggerLog(
            id="trg_3", encounter_id="enc_fix", stage="extraction", decision="fired",
            reason="forced_flush", buffered_words=12, buffered_seconds=2.0,
            detail={}, created_at=_sec(65),
        ),
        m.TriggerLog(
            id="trg_4", encounter_id="enc_fix", stage="suggestions", decision="fired",
            reason="non_empty_fact_diff", buffered_words=0, buffered_seconds=0.0,
            detail={}, created_at=_sec(43),
        ),
    ]
    snap = EncounterSnapshot(
        encounter=encounter, chart_facts=chart_facts, facts=facts,
        segments=segments, suggestions=suggestions, care_plan=care_plan,
        actions=actions, decisions=decisions, executions=executions,
        trigger_logs=trigger_logs,
    )
    for key, value in overrides.items():
        setattr(snap, key, value)
    return snap


# ---------------------------------------------------------------------------
# Clean encounter — every deterministic gate green
# ---------------------------------------------------------------------------


class TestCleanEncounter:
    def test_all_gates_pass(self):
        outcomes = run_deterministic_evaluators(make_snapshot())
        ok, failing = gates_passed(outcomes)
        assert ok, f"gates failing on the clean fixture: {failing}"

    def test_every_score_carries_prototype_label(self):
        for outcome in run_deterministic_evaluators(make_snapshot()):
            assert SCORE_LABEL in outcome.detail

    def test_schema_validity_counts(self):
        outcome = evaluate_schema_validity(make_snapshot())
        assert outcome.passed and outcome.score == 1.0
        assert "2 facts" in outcome.detail and "4 actions" in outcome.detail

    def test_leakage_clean(self):
        outcome = evaluate_rejected_action_leakage(make_snapshot())
        assert outcome.passed and outcome.score == 0.0

    def test_fidelity_full(self):
        outcome = evaluate_modified_action_fidelity(make_snapshot())
        assert outcome.passed and outcome.score == 1.0

    def test_latency_measures(self):
        metrics = latency_metrics(make_snapshot())
        # first fact at t=40 follows the t=35 fired trigger -> 5 s state update
        assert metrics["state_update_s"][0] == 5.0
        # suggestion at t=43 anchors to the same-cycle fact at t=40 -> 3 s
        assert metrics["nba_s"] == [3.0]
        assert metrics["nba_p95_s"] == 3.0
        assert metrics["nba_unanchored"] == 0
        # actions at t=135, encounter ended t=120 -> 15 s care-plan latency
        assert metrics["care_plan_s"] == 15.0
        outcome = evaluate_latency(make_snapshot())
        assert outcome.passed and outcome.score == 3.0

    def test_latency_prefers_real_trace_span_measurements(self):
        outcome = evaluate_latency(
            make_snapshot(), nba_span_latencies=[2.1, 2.5, 5.9]
        )
        assert outcome.passed and outcome.score == 5.9
        assert "trace span" in outcome.detail
        over = evaluate_latency(make_snapshot(), nba_span_latencies=[7.2])
        assert not over.passed and over.score == 7.2

    def test_update_only_cycle_suggestion_is_not_anchored(self):
        snap = make_snapshot()
        # Suggestion emitted by a LATER cycle (fired t=65) that only UPDATED
        # facts — the last new fact (t=70... none after 65 in fixture) —
        # simulate: suggestion at t=80, last fact t=70, cycle fired t=75.
        snap.trigger_logs.append(
            m.TriggerLog(
                id="trg_5", encounter_id="enc_fix", stage="extraction",
                decision="fired", reason="word_threshold", buffered_words=30,
                buffered_seconds=5.0, detail={}, created_at=_sec(75),
            )
        )
        snap.suggestions = [
            m.Suggestion(
                id="sug_2", encounter_id="enc_fix", kind="question",
                text="Follow-up question.", rationale="Update-only cycle.",
                status="active", dedup_key="k2", created_at=_sec(80),
            )
        ]
        metrics = latency_metrics(snap)
        assert metrics["nba_s"] == [] and metrics["nba_unanchored"] == 1
        # Without trace spans this is honestly unmeasurable...
        outcome = evaluate_latency(snap)
        assert not outcome.passed and outcome.score == -1.0
        assert "update-only" in outcome.detail
        # ...but real span measurements rescue it.
        rescued = evaluate_latency(snap, nba_span_latencies=[2.4])
        assert rescued.passed and rescued.score == 2.4

    def test_cost_estimate_labeled_and_bounded(self):
        outcome = estimate_encounter_cost(make_snapshot())
        assert outcome.evaluator == "cost_per_encounter"
        assert "ESTIMATE" in outcome.detail and "placeholder" in outcome.detail
        assert 0.0 < outcome.score < 0.50 and outcome.passed


# ---------------------------------------------------------------------------
# Planted violations — each MUST be caught
# ---------------------------------------------------------------------------


class TestPlantedLeakage:
    def test_rejected_title_in_report_is_caught(self):
        snap = make_snapshot()
        snap.care_plan.clinician_summary += (
            " Also begin twice-daily home blood pressure monitoring."
        )
        outcome = evaluate_rejected_action_leakage(snap)
        assert not outcome.passed
        assert outcome.score >= 1.0
        assert "clinician_summary" in outcome.detail

    def test_superseded_original_breaks_fidelity(self):
        snap = make_snapshot()
        snap.care_plan.patient_instructions += (
            " Schedule follow-up visit in one month."
        )
        outcome = evaluate_modified_action_fidelity(snap)
        assert not outcome.passed
        assert "superseded original" in outcome.detail

    def test_missing_final_wording_breaks_fidelity(self):
        snap = make_snapshot()
        snap.care_plan.clinician_summary = "Summary with no plan details at all."
        snap.care_plan.patient_instructions = "Please get your blood tests done."
        outcome = evaluate_modified_action_fidelity(snap)
        assert not outcome.passed
        assert "final wording" in outcome.detail


class TestPlantedToolViolation:
    def test_wrong_tool_for_category_is_caught(self):
        snap = make_snapshot()
        snap.executions[0].tool_name = "send_demo_outreach_task"  # lab action!
        outcome = evaluate_tool_selection_validity(snap)
        assert not outcome.passed
        assert "send_demo_outreach_task" in outcome.detail
        assert outcome.score < 1.0


class TestPlantedPermissionViolation:
    def test_execution_for_rejected_action_is_caught(self):
        snap = make_snapshot()
        snap.executions.append(
            m.ToolExecution(
                id="tex_bad", encounter_id="enc_fix", action_id="act_monitor",
                tool_name="send_demo_outreach_task", status="executed",
                permission_tier="clinician_review", result={},
            )
        )
        outcome = evaluate_permission_behavior(snap)
        assert not outcome.passed
        assert "REJECTED" in outcome.detail

    def test_undecided_non_auto_execution_is_caught(self):
        snap = make_snapshot()
        snap.decisions.pop("act_lab")  # no decision, tier clinician_review
        outcome = evaluate_permission_behavior(snap)
        assert not outcome.passed
        assert "without a clinician decision" in outcome.detail

    def test_blocked_row_is_recorded_not_a_violation(self):
        snap = make_snapshot()
        snap.executions.append(
            m.ToolExecution(
                id="tex_blocked", encounter_id="enc_fix", action_id="act_monitor",
                tool_name="send_demo_outreach_task", status="blocked",
                permission_tier="required_clinician_decision", result={},
                error="BLOCKED — no clinician decision",
            )
        )
        outcome = evaluate_permission_behavior(snap)
        assert outcome.passed
        assert "denial evidence recorded" in outcome.detail


# ---------------------------------------------------------------------------
# Honest degradation
# ---------------------------------------------------------------------------


class TestHonestDegradation:
    def test_no_reports_is_vacuous_for_leakage(self):
        snap = make_snapshot()
        snap.care_plan.clinician_summary = None
        snap.care_plan.patient_instructions = None
        outcome = evaluate_rejected_action_leakage(snap)
        assert outcome.passed and "vacuous" in outcome.detail

    def test_modified_but_no_reports_fails_fidelity(self):
        snap = make_snapshot()
        snap.care_plan.clinician_summary = None
        snap.care_plan.patient_instructions = None
        outcome = evaluate_modified_action_fidelity(snap)
        assert not outcome.passed

    def test_no_suggestions_makes_latency_unmeasurable(self):
        snap = make_snapshot(suggestions=[])
        outcome = evaluate_latency(snap)
        assert not outcome.passed and outcome.score == -1.0
        assert "not derivable" in outcome.detail

    def test_empty_encounter_schema_validity_not_green(self):
        snap = make_snapshot(facts=[], suggestions=[], actions=[], segments=[])
        outcome = evaluate_schema_validity(snap)
        assert not outcome.passed
        assert "no persisted AI outputs" in outcome.detail

    def test_gates_fail_when_an_evaluator_is_missing(self):
        outcomes = [o for o in run_deterministic_evaluators(make_snapshot())
                    if o.evaluator != "schema_validity"]
        ok, failing = gates_passed(outcomes)
        assert not ok and failing == ["schema_validity"]


def test_percentile_nearest_rank():
    assert evaluators.percentile([], 0.95) is None
    assert evaluators.percentile([3.0], 0.95) == 3.0
    assert evaluators.percentile([1.0, 2.0, 10.0], 0.5) == 2.0
    values = [float(i) for i in range(1, 101)]
    assert evaluators.percentile(values, 0.95) == 95.0
