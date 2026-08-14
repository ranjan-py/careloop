"""SQLAlchemy ORM models — the Postgres event/system record (spec §17).

Postgres is authoritative; Neo4j is an idempotent MERGE projection of these
rows (owned by app.context). String primary keys carry human-readable ids
(e.g. "enc_001") minted by the application. All patient data is synthetic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(DeclarativeBase):
    pass


class Clinician(Base):
    __tablename__ = "clinicians"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(255), default="Primary care clinician")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    age: Mapped[int] = mapped_column(Integer)
    sex: Mapped[str] = mapped_column(String(16))
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    medications: Mapped[list] = mapped_column(JSON, default=list)  # [{name, dose, ehr_status}]
    priorities: Mapped[list] = mapped_column(JSON, default=list)  # "What matters today?"
    care_gaps: Mapped[list] = mapped_column(JSON, default=list)
    appointment_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    display_only: Mapped[bool] = mapped_column(Boolean, default=False)  # spec §7 filler rows
    source_class: Mapped[str] = mapped_column(String(32), default="synthea_ehr")
    fhir_bundle_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TimelineEvent(Base):
    __tablename__ = "timeline_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(512))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class Encounter(Base):
    __tablename__ = "encounters"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    clinician_id: Mapped[str | None] = mapped_column(ForeignKey("clinicians.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="created")  # created|live|finalizing|finalized
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # live|replay
    # Trace-ID propagation (spec §20): minted at encounter start, all later
    # spans attach to this stored id explicitly.
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.id"), index=True)
    speaker: Mapped[str] = mapped_column(String(16))  # doctor|patient
    text: Mapped[str] = mapped_column(Text)
    ts: Mapped[float] = mapped_column(Float)  # seconds from session start
    is_final: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Fact(Base):
    __tablename__ = "facts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    encounter_id: Mapped[str | None] = mapped_column(ForeignKey("encounters.id"), nullable=True, index=True)
    fact_type: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(255))
    value: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(32))  # ehr|patient_report|clinician
    source_class: Mapped[str] = mapped_column(String(32))  # synthea_ehr|patient_report
    method: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. pharmacy_kiosk
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    verification_status: Mapped[str] = mapped_column(String(32), default="unverified")
    conflicts_with: Mapped[str | None] = mapped_column(String(64), nullable=True)  # fact id; never overwrite


class Suggestion(Base):
    __tablename__ = "suggestions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # question|info_gap|action
    text: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active")
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True)  # scoped PER ENCOUNTER (spec §9)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CarePlan(Base):
    __tablename__ = "care_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.id"), index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")  # draft|in_review|finalized
    clinician_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    patient_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    actions: Mapped[list[CarePlanAction]] = relationship(back_populates="care_plan")


class CarePlanAction(Base):
    __tablename__ = "care_plan_actions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    care_plan_id: Mapped[str] = mapped_column(ForeignKey("care_plans.id"), index=True)
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    patient_facts_used: Mapped[list] = mapped_column(JSON, default=list)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    risk_level: Mapped[str] = mapped_column(String(16))
    permission: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    care_plan: Mapped[CarePlan] = relationship(back_populates="actions")
    decisions: Mapped[list[Decision]] = relationship(back_populates="action")


class Decision(Base):
    """Clinician decision record (spec §14) — keeps original vs final visible."""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("care_plan_actions.id"), index=True)
    clinician_id: Mapped[str] = mapped_column(String(64))
    decision_type: Mapped[str] = mapped_column(String(16))  # approve|modify|reject
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # modify
    original_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    original_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    final_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # reject — value from the single shared RejectionCategory enum
    rejection_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    action: Mapped[CarePlanAction] = relationship(back_populates="decisions")


class ToolExecution(Base):
    __tablename__ = "tool_executions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    encounter_id: Mapped[str | None] = mapped_column(ForeignKey("encounters.id"), nullable=True, index=True)
    action_id: Mapped[str | None] = mapped_column(ForeignKey("care_plan_actions.id"), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))  # executed|failed|blocked
    permission_tier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvalResult(Base):
    __tablename__ = "eval_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.id"), index=True)
    evaluator: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))  # deterministic|model
    score: Mapped[float] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(Text, default="")
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceSnippet(Base):
    """Synthetic guideline/evidence snippets — the spec §11 retrieval corpus."""

    __tablename__ = "evidence_snippets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    topic_tags: Mapped[list] = mapped_column(JSON, default=list)


class TriggerLog(Base):
    """Every §9 trigger decision (fired/skipped + reason) — feeds §22 evals
    and §23 analytics. Written by app.ai.pipeline on each debounce evaluation."""

    __tablename__ = "trigger_log"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32))  # extraction|suggestions
    decision: Mapped[str] = mapped_column(String(16))  # fired|skipped
    reason: Mapped[str] = mapped_column(String(255))
    buffered_words: Mapped[int] = mapped_column(Integer, default=0)
    buffered_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FeedbackAnalyticsEvent(Base):
    """Synthetic historical recommendation events (spec §23 — 'Synthetic demo cohort')."""

    __tablename__ = "feedback_analytics"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    recommendation_title: Mapped[str] = mapped_column(String(512))
    decision: Mapped[str] = mapped_column(String(16))  # approved|modified|rejected
    rejection_category: Mapped[str | None] = mapped_column(String(64), nullable=True)  # shared enum value
    organization: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
