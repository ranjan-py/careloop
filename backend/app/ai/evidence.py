"""Evidence resolve route — serves the spec §11 retrieval corpus by id.

The corpus is seeded into Postgres from data/evidence/evidence_corpus.json;
retrieval (BM25) lives in the care-plan generation pipeline. This route lets
the frontend Evidence control render genuine snippet text + provenance for a
CarePlanAction's `evidence_refs`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import require_clinician
from app.db import models as m
from app.db.session import get_session
from app.schemas.core import EvidenceSnippet

router = APIRouter(prefix="/api/evidence", tags=["evidence"])


@router.get("")
async def get_evidence(
    ids: str = Query(..., description="Comma-separated evidence ids"),
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    if not wanted:
        return {"snippets": []}
    rows = (
        await session.execute(select(m.EvidenceSnippet).where(m.EvidenceSnippet.id.in_(wanted)))
    ).scalars().all()
    found = {r.id: r for r in rows}
    # Preserve request order; silently missing ids are omitted (caller shows
    # an honest "not found" state rather than fabricated evidence).
    snippets = [
        EvidenceSnippet.model_validate(found[i]).model_dump(mode="json")
        for i in wanted
        if i in found
    ]
    return {"snippets": snippets}
