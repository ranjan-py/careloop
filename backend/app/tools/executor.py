"""Tool execution with permission enforcement + persistence of ToolExecution rows.

The permission gate runs server-side before every invocation; a blocked
invocation is persisted (status "blocked") so the denial event shows up in
the trace summary and the AI Operations view (spec §15/§21A).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models as m
from app.tools.registry import CATEGORY_TOOL_MAP, TOOLS, ToolResult, check_permission


def _new_id() -> str:
    return f"tex_{uuid.uuid4().hex[:12]}"


async def execute_action_tool(
    session: AsyncSession,
    action: m.CarePlanAction,
    *,
    encounter_id: str | None,
    patient_id: str,
    tool_kwargs: dict | None = None,
) -> m.ToolExecution:
    """Run the tool mapped to an approved action, or record a blocked attempt.

    Returns the persisted ToolExecution row (caller commits).
    """
    tool_name = CATEGORY_TOOL_MAP.get(action.category, "generate_patient_instructions")
    permission = check_permission(action.permission, action.status)  # type: ignore[arg-type]

    if not permission.allowed:
        execution = m.ToolExecution(
            id=_new_id(),
            encounter_id=encounter_id,
            action_id=action.id,
            tool_name=tool_name,
            status="blocked",
            permission_tier=action.permission,
            result={},
            error=permission.reason,
        )
        session.add(execution)
        return execution

    tool = TOOLS[tool_name]
    kwargs = {"patient_id": patient_id, **(tool_kwargs or {})}
    try:
        result: ToolResult = tool(**_kwargs_for(tool_name, kwargs, action))
        execution = m.ToolExecution(
            id=_new_id(),
            encounter_id=encounter_id,
            action_id=action.id,
            tool_name=tool_name,
            status=result.status,
            permission_tier=action.permission,
            result=result.model_dump(mode="json"),
            error=None if result.status == "executed" else result.summary,
        )
        if result.status == "executed":
            action.status = "executed"
    except Exception as exc:  # noqa: BLE001 — mocked tools; record honestly, never crash the flow
        execution = m.ToolExecution(
            id=_new_id(),
            encounter_id=encounter_id,
            action_id=action.id,
            tool_name=tool_name,
            status="failed",
            permission_tier=action.permission,
            result={},
            error=str(exc),
        )
    session.add(execution)
    return execution


def _kwargs_for(tool_name: str, base: dict, action: m.CarePlanAction) -> dict:
    """Fill per-tool required kwargs from the action context."""
    if tool_name == "save_demo_medication_review":
        return {**base, "medication": base.get("medication", action.title)}
    if tool_name == "create_demo_referral":
        return {**base, "specialty": base.get("specialty", action.title)}
    if tool_name == "send_demo_outreach_task":
        return {**base, "purpose": base.get("purpose", action.title)}
    if tool_name == "generate_patient_instructions":
        return {**base, "care_plan_id": action.care_plan_id}
    return base
