"""AI Operations trace summary — GET /api/ops/trace-summary/{encounter_id}.

Reads the app's own Postgres records, NOT live Langfuse queries (v3 ingestion
is async — spec §19/§21A). Rows are computed from what actually happened;
steps with no evidence report DEGRADED/FAIL honestly. Permission denials
(blocked tool executions) must be visible here (spec §15).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import require_clinician
from app.db import models as m
from app.db.session import get_session
from app.schemas.core import TraceSummaryRow

router = APIRouter(prefix="/api/ops", tags=["observability"])


@router.get("/trace-summary/{encounter_id}")
async def trace_summary(
    encounter_id: str,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    encounter = await session.get(m.Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Encounter not found")

    async def count(stmt) -> int:
        return (await session.execute(stmt)).scalar_one()

    segment_count = await count(
        select(func.count()).where(m.TranscriptSegment.encounter_id == encounter_id)
    )
    fact_count = await count(select(func.count()).where(m.Fact.encounter_id == encounter_id))
    care_plan = (
        await session.execute(select(m.CarePlan).where(m.CarePlan.encounter_id == encounter_id))
    ).scalar_one_or_none()
    eval_count = await count(select(func.count()).where(m.EvalResult.encounter_id == encounter_id))
    decision_count = 0
    action_count = 0
    if care_plan is not None:
        action_ids = (
            await session.execute(
                select(m.CarePlanAction.id).where(m.CarePlanAction.care_plan_id == care_plan.id)
            )
        ).scalars().all()
        action_count = len(action_ids)
        if action_ids:
            decision_count = await count(
                select(func.count()).where(m.Decision.action_id.in_(action_ids))
            )
    executions = (
        await session.execute(select(m.ToolExecution).where(m.ToolExecution.encounter_id == encounter_id))
    ).scalars().all()

    def row(step: str, ok: bool, detail_ok: str, detail_bad: str) -> TraceSummaryRow:
        return TraceSummaryRow(
            step=step,
            status="PASS" if ok else "DEGRADED",
            detail=detail_ok if ok else detail_bad,
        )

    rows: list[TraceSummaryRow] = [
        row(
            "Context load", True,
            f"Encounter {encounter.id} (trace {encounter.trace_id or 'unset'})",
            "",
        ),
        row(
            "Live transcription", segment_count > 0,
            f"{segment_count} finalized segments persisted",
            "No transcript segments persisted (Deepgram relay not wired / no session run)",
        ),
        row(
            "Fact extraction", fact_count > 0,
            f"{fact_count} encounter facts persisted",
            "No encounter facts (AI extraction not wired / no session run)",
        ),
        row(
            "Care-plan generation", care_plan is not None and action_count > 0,
            f"Care plan {(care_plan.id if care_plan else '')} with {action_count} actions",
            "No generated care-plan actions yet",
        ),
        row(
            "Safety/schema eval", eval_count > 0,
            f"{eval_count} evaluator results stored",
            "No evaluator results yet",
        ),
        row(
            "Clinician feedback captured", decision_count > 0,
            f"{decision_count} clinician decisions recorded",
            "No clinician decisions recorded yet",
        ),
    ]

    executed = [e for e in executions if e.status == "executed"]
    blocked = [e for e in executions if e.status == "blocked"]
    failed = [e for e in executions if e.status == "failed"]
    if executions:
        rows.append(
            TraceSummaryRow(
                step="Tool execution",
                status="PASS" if executed and not failed else ("FAIL" if failed else "DEGRADED"),
                detail=f"{len(executed)} executed, {len(blocked)} blocked, {len(failed)} failed",
            )
        )
    else:
        rows.append(
            TraceSummaryRow(step="Tool execution", status="DEGRADED", detail="No tool executions yet")
        )
    # Permission denials get their own visible rows (spec §15/§21A).
    for e in blocked:
        rows.append(
            TraceSummaryRow(
                step=f"Permission gate — {e.tool_name}",
                status="BLOCKED",
                detail=e.error or f"Invocation above permission tier {e.permission_tier}",
            )
        )

    return {"rows": [r.model_dump(mode="json") for r in rows]}
