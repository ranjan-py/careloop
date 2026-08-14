"""Contract-schema validation tests (CONTRACTS.md core models + WS envelopes)."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.core import (
    CarePlanAction,
    ClientMessageAdapter,
    ConnStatusMessage,
    Fact,
    ServerMessageAdapter,
    SessionStartMessage,
    Suggestion,
    TraceSummaryRow,
)

NOW = datetime.now(timezone.utc)


def make_fact(**overrides) -> Fact:
    payload = {
        "id": "fact_001",
        "fact_type": "medication_status",
        "subject": "lisinopril",
        "value": "stopped",
        "source_type": "patient_report",
        "source_class": "patient_report",
        "method": "pharmacy_kiosk",
        "reported_at": NOW,
        "ingested_at": NOW,
        "confidence": 0.92,
        "verification_status": "unverified",
        "encounter_id": "enc_001",
        "conflicts_with": "fact_ehr_lisinopril_active",
    }
    payload.update(overrides)
    return Fact(**payload)


class TestFact:
    def test_valid_fact_round_trips(self):
        fact = make_fact()
        assert fact.source_class == "patient_report"
        assert Fact.model_validate(fact.model_dump()) == fact

    def test_invalid_source_class_rejected(self):
        with pytest.raises(ValidationError):
            make_fact(source_class="hand_authored")  # not a contract value

    def test_confidence_bounds_enforced(self):
        with pytest.raises(ValidationError):
            make_fact(confidence=1.5)

    def test_optional_fields_default_to_none(self):
        fact = make_fact(method=None, encounter_id=None, conflicts_with=None)
        assert fact.method is None and fact.conflicts_with is None


class TestCarePlanAction:
    def test_valid_action(self):
        action = CarePlanAction(
            id="act_001",
            care_plan_id="cp_001",
            category="medication",
            title="Review alternative BP therapy",
            description="Patient reports stopping lisinopril due to dizziness.",
            rationale="Medication conflict between EHR and patient report.",
            patient_facts_used=["fact_001"],
            evidence_refs=["ev_htn_004"],
            risk_level="high",
            permission="required_clinician_decision",
            status="pending",
        )
        assert action.decision is None
        assert action.permission == "required_clinician_decision"

    def test_invalid_permission_tier_rejected(self):
        with pytest.raises(ValidationError):
            CarePlanAction(
                id="act_001",
                care_plan_id="cp_001",
                category="medication",
                title="t",
                description="d",
                rationale="r",
                risk_level="high",
                permission="fully_autonomous",  # not a contract tier
                status="pending",
            )


class TestSuggestion:
    def test_valid_suggestion(self):
        s = Suggestion(
            id="sug_001",
            encounter_id="enc_001",
            kind="info_gap",
            text="Potassium is 92 days old — confirm whether a newer result exists.",
            rationale="Stale renal labs before any medication change.",
            created_at=NOW,
            status="active",
        )
        assert s.kind == "info_gap"


class TestTraceSummaryRow:
    def test_all_contract_statuses_accepted(self):
        for status in ("PASS", "FAIL", "DEGRADED", "BLOCKED", "DENIED"):
            assert TraceSummaryRow(step="x", status=status, detail="").status == status

    def test_unknown_status_rejected(self):
        with pytest.raises(ValidationError):
            TraceSummaryRow(step="x", status="MAYBE", detail="")


class TestWsEnvelopes:
    def test_client_union_discriminates_session_start(self):
        message = ClientMessageAdapter.validate_python(
            {"type": "session.start", "mode": "replay", "ticket": "abc"}
        )
        assert isinstance(message, SessionStartMessage)
        assert message.mode == "replay"

    def test_client_union_rejects_unknown_type(self):
        with pytest.raises(ValidationError):
            ClientMessageAdapter.validate_python({"type": "session.pause"})

    def test_speaker_correct_requires_contract_speaker(self):
        with pytest.raises(ValidationError):
            ClientMessageAdapter.validate_python(
                {"type": "speaker.correct", "segment_id": "seg_1", "speaker": "nurse"}
            )

    def test_server_union_discriminates_conn_status(self):
        message = ServerMessageAdapter.validate_python(
            {"type": "conn.status", "deepgram": "reconnecting", "detail": "retry 2/4"}
        )
        assert isinstance(message, ConnStatusMessage)

    def test_server_state_fact_carries_full_fact(self):
        message = ServerMessageAdapter.validate_python(
            {"type": "state.fact", "fact": make_fact().model_dump(), "change": "disputed"}
        )
        assert message.fact.subject == "lisinopril"
        assert message.change == "disputed"
