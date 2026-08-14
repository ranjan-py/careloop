"""Eval routes — GET /api/evals/{encounter_id} (spec §21C/§22).

Returns the stored per-session evaluator results (written by
``app.evals.evaluators.run_online_evals``) plus the launch-criteria table
computed from those real rows (``app.evals.criteria``). Criteria without a
measurement report "not yet measured" — honest red, never fabricated. All
scores are labeled "Prototype evaluator — not clinical validation".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import require_clinician
from app.db import models as m
from app.db.session import get_session
from app.evals.criteria import build_launch_criteria
from app.evals.evaluators import SCORE_LABEL
from app.schemas.core import EvalResult

router = APIRouter(prefix="/api/evals", tags=["evals"])


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
            .order_by(m.EvalResult.created_at, m.EvalResult.id)
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
    launch_criteria = build_launch_criteria(rows)

    return {
        "results": [r.model_dump(mode="json") for r in results],
        "launch_criteria": [c.model_dump(mode="json") for c in launch_criteria],
        "score_label": SCORE_LABEL,
    }
