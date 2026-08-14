"""Contract models — EXACT mirror of contracts/CONTRACTS.md "Core models".

Frontend mirror: frontend/src/lib/types.ts. Any change here requires a contract
change and a matching frontend update. All data in this demo is synthetic
("Synthetic clinical AI prototype — not for patient care").
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# ---------------------------------------------------------------------------
# Shared literals / enums
# ---------------------------------------------------------------------------

FactType = Literal[
    "medication_status", "observation", "symptom", "lab", "condition", "care_gap"
]
SourceType = Literal["ehr", "patient_report", "clinician"]
SourceClass = Literal["synthea_ehr", "patient_report"]  # spec §6.1
VerificationStatus = Literal["unverified", "confirmed", "disputed"]

SuggestionKind = Literal["question", "info_gap", "action"]
SuggestionStatus = Literal["active", "dismissed", "superseded"]

ActionCategory = Literal[
    "lab", "medication", "monitoring", "follow_up", "referral", "other"
]
RiskLevel = Literal["low", "medium", "high"]
PermissionTier = Literal["auto_demo", "clinician_review", "required_clinician_decision"]
ActionStatus = Literal[
    "pending", "approved", "modified", "rejected", "executed", "denied"
]

Speaker = Literal["doctor", "patient"]
TraceStepStatus = Literal["PASS", "FAIL", "DEGRADED", "BLOCKED", "DENIED"]


class RejectionCategory(str, enum.Enum):
    """SINGLE shared rejection enum (spec §14) — imported everywhere, never redefined.

    Exact wire values and display labels per contracts/CONTRACTS.md.
    """

    MISSING_PATIENT_INFORMATION = "missing_patient_information"
    CLINICAL_DISAGREEMENT = "clinical_disagreement"
    PATIENT_LIMITATION = "patient_limitation"
    OPERATIONAL_WORKFLOW_LIMITATION = "operational_workflow_limitation"
    ORGANIZATION_PROTOCOL_CONSTRAINT = "organization_protocol_constraint"
    REQUIRES_SUPERVISION_ESCALATION = "requires_supervision_escalation"
    OTHER = "other"

    @property
    def display_label(self) -> str:
        return REJECTION_CATEGORY_LABELS[self]


REJECTION_CATEGORY_LABELS: dict[RejectionCategory, str] = {
    RejectionCategory.MISSING_PATIENT_INFORMATION: "Missing patient information",
    RejectionCategory.CLINICAL_DISAGREEMENT: "Clinical disagreement",
    RejectionCategory.PATIENT_LIMITATION: "Patient limitation",
    RejectionCategory.OPERATIONAL_WORKFLOW_LIMITATION: "Operational/workflow limitation",
    RejectionCategory.ORGANIZATION_PROTOCOL_CONSTRAINT: "Organization/protocol constraint",
    RejectionCategory.REQUIRES_SUPERVISION_ESCALATION: "Requires supervision/escalation",
    RejectionCategory.OTHER: "Other",
}


# ---------------------------------------------------------------------------
# Core clinical models
# ---------------------------------------------------------------------------


class Fact(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    fact_type: FactType
    subject: str
    value: str
    source_type: SourceType
    source_class: SourceClass
    method: str | None = None  # e.g. "pharmacy_kiosk"
    reported_at: datetime
    ingested_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)
    verification_status: VerificationStatus
    encounter_id: str | None = None
    conflicts_with: str | None = None  # fact id


class Suggestion(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    encounter_id: str
    kind: SuggestionKind
    text: str
    rationale: str
    created_at: datetime
    status: SuggestionStatus


class Decision(BaseModel):
    """Clinician decision attached to a CarePlanAction (spec §14)."""

    model_config = ConfigDict(from_attributes=True)

    clinician_id: str
    decided_at: datetime
    model_version: str | None = None
    prompt_version: str | None = None
    # modify
    final_title: str | None = None
    final_description: str | None = None
    reason: str | None = None
    # reject
    category: RejectionCategory | None = None
    remarks: str | None = None


class CarePlanAction(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    care_plan_id: str
    category: ActionCategory
    title: str
    description: str
    rationale: str
    patient_facts_used: list[str] = Field(default_factory=list)  # fact ids
    evidence_refs: list[str] = Field(default_factory=list)  # evidence ids (spec §11)
    risk_level: RiskLevel
    permission: PermissionTier
    status: ActionStatus
    decision: Decision | None = None


# ---------------------------------------------------------------------------
# Evals / observability rows
# ---------------------------------------------------------------------------


class EvalResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evaluator: str
    kind: Literal["deterministic", "model"]
    score: float
    passed: bool
    detail: str
    encounter_id: str
    prompt_version: str | None = None


class CriterionRow(BaseModel):
    """Launch-criteria table row (spec §22.1)."""

    name: str
    target: str
    actual: str
    passed: bool


class TraceSummaryRow(BaseModel):
    step: str
    status: TraceStepStatus
    detail: str
    latency_ms: float | None = None


# ---------------------------------------------------------------------------
# Patients / timeline
# (CONTRACTS.md names these models but does not pin their fields; shapes below
# follow spec §7 and are the backend/frontend meeting point — keep in sync.)
# ---------------------------------------------------------------------------


class MedicationSummary(BaseModel):
    name: str
    dose: str | None = None
    ehr_status: str = "active"


class LabSummary(BaseModel):
    name: str
    value: str
    unit: str | None = None
    observed_at: datetime


class PatientListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    age: int
    sex: str
    conditions: list[str] = Field(default_factory=list)
    appointment_time: datetime | None = None
    attention_count: int = 0  # "3–4 items need attention"
    display_only: bool = False  # spec §7: filler rows are non-interactive


class PriorityItem(BaseModel):
    """One 'What matters today?' item (spec §7); severity drives the UI chip."""

    id: str
    label: str
    detail: str | None = None
    severity: RiskLevel | None = None


class CareGap(BaseModel):
    id: str
    label: str
    detail: str | None = None


class PatientDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    age: int
    sex: str
    conditions: list[str] = Field(default_factory=list)
    medications: list[MedicationSummary] = Field(default_factory=list)
    labs: list[LabSummary] = Field(default_factory=list)
    priorities: list[PriorityItem] = Field(default_factory=list)  # "What matters today?"
    care_gaps: list[CareGap] = Field(default_factory=list)


class EvidenceSnippet(BaseModel):
    """Synthetic guideline/evidence snippet (spec §11 retrieval corpus)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    body: str
    topic_tags: list[str] = Field(default_factory=list)


class TimelineEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_type: Literal["lab", "vital", "medication", "condition", "encounter", "care_gap", "note"]
    label: str
    occurred_at: datetime  # runtime-relative seeding; display ages derived at render time
    detail: str | None = None


# ---------------------------------------------------------------------------
# Clinician (auth surface)
# ---------------------------------------------------------------------------


class Clinician(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    name: str
    role: str


# ---------------------------------------------------------------------------
# WebSocket envelopes — /api/encounters/{id}/stream (CONTRACTS.md WS protocol)
# Text frames both directions; discriminated on "type".
# ---------------------------------------------------------------------------


class TranscriptSegment(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    speaker: Speaker
    text: str
    ts: float  # seconds from session start (contract leaves ts untyped; float chosen, see report)


# Client → server


class SessionStartMessage(BaseModel):
    type: Literal["session.start"] = "session.start"
    mode: Literal["live", "replay"]
    ticket: str  # one-time, short-lived; issued by POST /api/encounters


class SpeakerCorrectMessage(BaseModel):
    type: Literal["speaker.correct"] = "speaker.correct"
    segment_id: str
    speaker: Speaker


class SessionEndMessage(BaseModel):
    type: Literal["session.end"] = "session.end"


class SuggestionDismissMessage(BaseModel):
    """Clinician dismissed a suggestion (spec §8C: dismissals are logged)."""

    type: Literal["suggestion.dismiss"] = "suggestion.dismiss"
    suggestion_id: str


ClientMessage = Annotated[
    Union[
        SessionStartMessage,
        SpeakerCorrectMessage,
        SessionEndMessage,
        SuggestionDismissMessage,
    ],
    Field(discriminator="type"),
]
ClientMessageAdapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# Server → client


class TranscriptInterimMessage(BaseModel):
    type: Literal["transcript.interim"] = "transcript.interim"
    segment: TranscriptSegment


class TranscriptFinalMessage(BaseModel):
    type: Literal["transcript.final"] = "transcript.final"
    segment: TranscriptSegment


class StateFactMessage(BaseModel):
    type: Literal["state.fact"] = "state.fact"
    fact: Fact
    change: Literal["added", "updated", "disputed"]


class SuggestionActiveMessage(BaseModel):
    type: Literal["suggestion.active"] = "suggestion.active"
    suggestion: Suggestion


class SuggestionRemoveMessage(BaseModel):
    type: Literal["suggestion.remove"] = "suggestion.remove"
    suggestion_id: str


class ConnStatusMessage(BaseModel):
    type: Literal["conn.status"] = "conn.status"
    deepgram: Literal["connected", "reconnecting", "degraded", "error"]
    detail: str = ""


class SessionFinalizingMessage(BaseModel):
    type: Literal["session.finalizing"] = "session.finalizing"


class SessionFinalizedMessage(BaseModel):
    type: Literal["session.finalized"] = "session.finalized"
    encounter_id: str


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    scope: Literal["deepgram", "openai", "internal"]
    message: str
    recoverable: bool


ServerMessage = Annotated[
    Union[
        TranscriptInterimMessage,
        TranscriptFinalMessage,
        StateFactMessage,
        SuggestionActiveMessage,
        SuggestionRemoveMessage,
        ConnStatusMessage,
        SessionFinalizingMessage,
        SessionFinalizedMessage,
        ErrorMessage,
    ],
    Field(discriminator="type"),
]
ServerMessageAdapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
