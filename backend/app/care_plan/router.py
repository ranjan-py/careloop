"""Care-plan decision routes — approve / modify / reject / finalize (spec §13–§16).

Every decision persists an immutable Decision row (original vs final visible);
finalize runs the mocked tools through the server-side permission gate so
blocked attempts are recorded, then returns the contract payload. Report text
(clinician_summary / patient_instructions) comes from the real OpenAI pipeline
in a later milestone — returned as null here, never faked.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.extraction import FactView
from app.ai.reports import (
    DecidedAction,
    RejectedAction,
    ReportLeakageError,
    generate_reports,
)
from app.auth.session import require_clinician
from app.config import get_settings
from app.db import models as m
from app.db.session import get_session
from app.schemas.core import CarePlanAction, Clinician, Decision, RejectionCategory
from app.tools.executor import execute_action_tool

router = APIRouter(prefix="/api/care-plan", tags=["care-plan"])


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def _get_action_or_404(session: AsyncSession, action_id: str) -> m.CarePlanAction:
    action = await session.get(m.CarePlanAction, action_id)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Care-plan action not found")
    return action


async def _latest_decision(session: AsyncSession, action_id: str) -> m.Decision | None:
    return (
        await session.execute(
            select(m.Decision)
            .where(m.Decision.action_id == action_id)
            .order_by(m.Decision.decided_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _action_payload(action: m.CarePlanAction, decision: m.Decision | None) -> dict:
    decision_model = None
    if decision is not None:
        decision_model = Decision(
            clinician_id=decision.clinician_id,
            decided_at=decision.decided_at,
            model_version=decision.model_version,
            prompt_version=decision.prompt_version,
            final_title=decision.final_title,
            final_description=decision.final_description,
            reason=decision.reason,
            category=RejectionCategory(decision.rejection_category) if decision.rejection_category else None,
            remarks=decision.remarks,
        )
    payload = CarePlanAction.model_validate(action).model_copy(update={"decision": decision_model})
    return payload.model_dump(mode="json")


def _base_decision(action: m.CarePlanAction, clinician: Clinician, decision_type: str) -> m.Decision:
    settings = get_settings()
    return m.Decision(
        id=_new_id("dec"),
        action_id=action.id,
        clinician_id=clinician.id,
        decision_type=decision_type,
        model_version=settings.openai_model,
        prompt_version=None,  # populated once Langfuse prompt management is wired (spec §19)
        original_title=action.title,
        original_description=action.description,
        decided_at=datetime.now(timezone.utc),
    )


@router.post("/actions/{action_id}/approve")
async def approve_action(
    action_id: str,
    session: AsyncSession = Depends(get_session),
    clinician: Clinician = Depends(require_clinician),
) -> dict:
    action = await _get_action_or_404(session, action_id)
    decision = _base_decision(action, clinician, "approve")
    action.status = "approved"
    session.add(decision)
    await session.commit()
    return {"action": _action_payload(action, decision)}


class ModifyRequest(BaseModel):
    final_title: str
    final_description: str
    reason: str


@router.post("/actions/{action_id}/modify")
async def modify_action(
    action_id: str,
    body: ModifyRequest,
    session: AsyncSession = Depends(get_session),
    clinician: Clinician = Depends(require_clinician),
) -> dict:
    action = await _get_action_or_404(session, action_id)
    decision = _base_decision(action, clinician, "modify")
    decision.final_title = body.final_title
    decision.final_description = body.final_description
    decision.reason = body.reason
    # Action row carries the clinician's FINAL version; the Decision row
    # preserves the superseded original (modified-action fidelity, spec §22.1).
    action.title = body.final_title
    action.description = body.final_description
    action.status = "modified"
    session.add(decision)
    await session.commit()
    return {"action": _action_payload(action, decision)}


class RejectRequest(BaseModel):
    category: RejectionCategory  # single shared enum — spec §14
    remarks: str = ""


@router.post("/actions/{action_id}/reject")
async def reject_action(
    action_id: str,
    body: RejectRequest,
    session: AsyncSession = Depends(get_session),
    clinician: Clinician = Depends(require_clinician),
) -> dict:
    action = await _get_action_or_404(session, action_id)
    decision = _base_decision(action, clinician, "reject")
    decision.rejection_category = body.category.value
    decision.remarks = body.remarks
    action.status = "rejected"
    session.add(decision)
    await session.commit()
    # UI displays: "Feedback captured. No automatic model change is made." (spec §14)
    return {"action": _action_payload(action, decision)}


@router.post("/{care_plan_id}/finalize")
async def finalize_care_plan(
    care_plan_id: str,
    session: AsyncSession = Depends(get_session),
    _: Clinician = Depends(require_clinician),
) -> dict:
    care_plan = await session.get(m.CarePlan, care_plan_id)
    if care_plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Care plan not found")

    actions = (
        await session.execute(select(m.CarePlanAction).where(m.CarePlanAction.care_plan_id == care_plan_id))
    ).scalars().all()

    executions: list[m.ToolExecution] = []
    if care_plan.status == "finalized":
        # Idempotent: re-finalizing never re-runs tools; return stored executions
        # (frontend summary page calls finalize after the care-plan page did).
        executions = (
            await session.execute(
                select(m.ToolExecution).where(m.ToolExecution.encounter_id == care_plan.encounter_id)
            )
        ).scalars().all()
    else:
        for action in actions:
            if action.status in ("rejected", "executed"):
                continue  # rejected actions never execute; executed are done
            execution = await execute_action_tool(
                session,
                action,
                encounter_id=care_plan.encounter_id,
                patient_id=care_plan.patient_id,
            )
            executions.append(execution)

        # Spec §16: REAL report generation from the final clinician-decided
        # plan — modified actions in FINAL wording, rejected as exclusions.
        decided_inputs: list[DecidedAction] = []
        rejected_inputs: list[RejectedAction] = []
        for action in actions:
            decision = await _latest_decision(session, action.id)
            if action.status == "rejected":
                rejected_inputs.append(RejectedAction(title=action.title, category=action.category))
                continue
            was_modified = bool(decision and decision.final_title)
            decided_inputs.append(
                DecidedAction(
                    category=action.category,
                    title=(decision.final_title if was_modified else action.title),
                    description=(
                        decision.final_description
                        if decision and decision.final_description
                        else action.description
                    ),
                    status="modified" if was_modified else "approved",
                    original_title=(
                        action.title
                        if was_modified and decision.final_title != action.title
                        else None
                    ),
                )
            )
        encounter = await session.get(m.Encounter, care_plan.encounter_id)
        fact_rows = (
            await session.execute(
                select(m.Fact).where(
                    m.Fact.patient_id == care_plan.patient_id,
                    (m.Fact.encounter_id == care_plan.encounter_id)
                    | (m.Fact.encounter_id.is_(None)),
                )
            )
        ).scalars().all()
        fact_views = [
            FactView(
                id=f.id,
                fact_type=f.fact_type,
                subject=f.subject,
                value=f.value,
                source_type=f.source_type,
                source_class=f.source_class,
                verification_status=f.verification_status,
                method=f.method,
                encounter_id=f.encounter_id,
                conflicts_with=f.conflicts_with,
            )
            for f in fact_rows
        ]
        try:
            bundle = await generate_reports(
                encounter_id=care_plan.encounter_id,
                patient_id=care_plan.patient_id,
                facts=fact_views,
                decided_actions=decided_inputs,
                rejected_actions=rejected_inputs,
                trace_id=encounter.trace_id if encounter else None,
            )
        except ReportLeakageError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=f"Report generation failed leakage check: {exc}",
            ) from None
        except Exception as exc:  # noqa: BLE001 — honest failure, never canned
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=f"Report generation failed: {type(exc).__name__}: {exc}",
            ) from None
        care_plan.clinician_summary = bundle.clinician_summary
        care_plan.patient_instructions = bundle.patient_instructions

        care_plan.status = "finalized"
        care_plan.finalized_at = datetime.now(timezone.utc)
        await session.commit()

    action_payloads = []
    for action in actions:
        decision = await _latest_decision(session, action.id)
        action_payloads.append(_action_payload(action, decision))

    return {
        "care_plan": {
            "id": care_plan.id,
            "encounter_id": care_plan.encounter_id,
            "patient_id": care_plan.patient_id,
            "status": care_plan.status,
            "actions": action_payloads,
        },
        "executions": [
            {
                "id": e.id,
                "action_id": e.action_id,
                "tool_name": e.tool_name,
                "status": e.status,
                "permission_tier": e.permission_tier,
                "result": e.result,
                "error": e.error,
            }
            for e in executions
        ],
        # Report generation is the real-OpenAI milestone (spec §16) — null until
        # wired, never fabricated text.
        "clinician_summary": care_plan.clinician_summary,
        "patient_instructions": care_plan.patient_instructions,
    }
