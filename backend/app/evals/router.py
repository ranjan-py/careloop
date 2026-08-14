"""Eval routes — GET /api/evals/{encounter_id} (spec §22).

Returns stored per-session evaluator results plus the launch-criteria table.
Evaluator implementations land with the eval milestone; until rows exist the
criteria report honestly as not-yet-measured. All scores are labeled
"Prototype evaluator — not clinical validation".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import require_clinician
from app.db import models as m
from app.db.session import get_session
from app.schemas.core import CriterionRow, EvalResult

router = APIRouter(prefix="/api/evals", tags=["evals"])

SCORE_LABEL = "Prototype evaluator — not clinical validation"


def _criterion(name: str, target: str, results: dict[str, m.EvalResult]) -> CriterionRow:
    row = results.get(name)
    if row is None:
        return CriterionRow(name=name, target=target, actual="not yet measured", passed=False)
    return CriterionRow(name=name, target=target, actual=f"{row.score:g}", passed=row.passed)


@router.get("/{encounter_id}")
async def get_evals(
    encounter_id: str,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    rows = (
        await session.execute(
            select(m.EvalResult)
            .where(m.EvalResult.encounter_id == encounter_id)
            .order_by(m.EvalResult.created_at)
        )
    ).scalars().all()

    results = [
        EvalResult(
            evaluator=r.evaluator,
            kind=r.kind,  # type: ignore[arg-type]
            score=r.score,
            passed=r.passed,
            detail=r.detail,
            encounter_id=r.encounter_id,
            prompt_version=r.prompt_version,
        )
        for r in rows
    ]

    by_name = {r.evaluator: r for r in rows}
    # Launch-criteria gate (spec §22.1) — explicit targets, honest actuals.
    launch_criteria = [
        _criterion("schema_validity", "100%", by_name),
        _criterion("unsupported_fact_rate", "0%", by_name),
        _criterion("rejected_action_leakage", "0", by_name),
        _criterion("modified_action_fidelity", "100%", by_name),
        _criterion("p95_next_best_action_latency", "< 6 s", by_name),
        _criterion("cost_per_encounter", "< $0.50", by_name),
    ]

    return {
        "results": [r.model_dump(mode="json") for r in results],
        "launch_criteria": [c.model_dump(mode="json") for c in launch_criteria],
        "score_label": SCORE_LABEL,
    }
