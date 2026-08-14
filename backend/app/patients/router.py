"""Patient routes — list, detail, timeline. Reads from app Postgres.

Seeding (Synthea → overlay → Postgres) is owned by the data/seed pipeline;
these routes render whatever rows exist and return empty lists otherwise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import require_clinician
from app.db import models as m
from app.db.session import get_session
from app.schemas.core import PatientDetail, PatientListItem, TimelineEvent

router = APIRouter(prefix="/api/patients", tags=["patients"])


def _attention_count(patient: m.Patient) -> int:
    return len(patient.priorities or [])


@router.get("")
async def list_patients(
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    rows = (await session.execute(select(m.Patient).order_by(m.Patient.appointment_time))).scalars().all()
    patients = [
        PatientListItem(
            id=p.id,
            name=p.name,
            age=p.age,
            sex=p.sex,
            conditions=p.conditions or [],
            appointment_time=p.appointment_time,
            attention_count=_attention_count(p),
            display_only=bool(p.display_only),
        )
        for p in rows
    ]
    return {"patients": [p.model_dump(mode="json") for p in patients]}


async def _get_patient_or_404(session: AsyncSession, patient_id: str) -> m.Patient:
    patient = await session.get(m.Patient, patient_id)
    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Patient not found")
    return patient


@router.get("/{patient_id}")
async def get_patient(
    patient_id: str,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    p = await _get_patient_or_404(session, patient_id)
    # Labs render from stored fact rows (source-classed, runtime-relative timestamps).
    lab_facts = (
        await session.execute(
            select(m.Fact)
            .where(m.Fact.patient_id == patient_id, m.Fact.fact_type == "lab")
            .order_by(m.Fact.reported_at.desc())
        )
    ).scalars().all()
    detail = PatientDetail(
        id=p.id,
        name=p.name,
        age=p.age,
        sex=p.sex,
        conditions=p.conditions or [],
        medications=p.medications or [],
        labs=[
            {"name": f.subject, "value": f.value, "unit": None, "observed_at": f.reported_at}
            for f in lab_facts
        ],
        priorities=p.priorities or [],
        care_gaps=p.care_gaps or [],
    )
    return {"patient": detail.model_dump(mode="json")}


@router.get("/{patient_id}/timeline")
async def get_timeline(
    patient_id: str,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    await _get_patient_or_404(session, patient_id)
    # Recent-first, capped: the Synthea base chart carries decades of background
    # encounters that would drown the scripted story (observed in UI walkthrough).
    cutoff = datetime.now(timezone.utc) - timedelta(days=450)
    rows = (
        await session.execute(
            select(m.TimelineEvent)
            .where(
                m.TimelineEvent.patient_id == patient_id,
                m.TimelineEvent.occurred_at >= cutoff,
            )
            .order_by(m.TimelineEvent.occurred_at.desc())
            .limit(20)
        )
    ).scalars().all()
    events = [TimelineEvent.model_validate(r) for r in rows]
    return {"events": [e.model_dump(mode="json") for e in events]}
