"""Patient routes — list, detail, timeline. Reads from app Postgres.

Seeding (Synthea → overlay → Postgres) is owned by the data/seed pipeline;
these routes render whatever rows exist and return empty lists otherwise.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import require_clinician
from app.db import models as m
from app.db.session import get_session
from app.schemas.core import (
    BloodPressureReading,
    LabSummary,
    PatientDetail,
    PatientListItem,
    TimelineEvent,
)

router = APIRouter(prefix="/api/patients", tags=["patients"])

# "148/92 mmHg", "around 150/95 mmHg" → (148, 92) / (150, 95). Encounter speech
# also yields BP facts with no numbers at all ("creeping back up"); those stay
# in the fact list and are simply not plottable.
_BP_VALUE = re.compile(r"(\d{2,3})\s*/\s*(\d{2,3})")
_BP_SUBJECT = "blood pressure"


def _attention_count(patient: m.Patient) -> int:
    return len(patient.priorities or [])


def blood_pressure_reading(fact: m.Fact) -> BloodPressureReading | None:
    """A BP observation fact as a plottable point, or None if it isn't one.

    Returns None for non-BP observations and for BP facts with no numbers in
    them — encounter speech produces plenty ("creeping back up"), and a chart
    must not invent a value for those.
    """
    if fact.fact_type != "observation" or fact.subject.strip().lower() != _BP_SUBJECT:
        return None
    match = _BP_VALUE.search(fact.value)
    if match is None:
        return None
    return BloodPressureReading(
        id=fact.id,
        systolic=int(match.group(1)),
        diastolic=int(match.group(2)),
        value=fact.value,
        observed_at=fact.reported_at,
        source_type=fact.source_type,
        source_class=fact.source_class,
        method=fact.method,
        encounter_id=fact.encounter_id,
    )


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
            mrn=p.mrn,
            birth_date=p.birth_date,
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
    # Labs and vitals render from stored fact rows (source-classed,
    # runtime-relative timestamps). Both lists carry provenance: an encounter
    # can write lab/observation facts for this patient (spec §10), and the
    # pre-visit chart must never present those as EHR results.
    facts = (
        await session.execute(
            select(m.Fact)
            .where(
                m.Fact.patient_id == patient_id,
                m.Fact.fact_type.in_(("lab", "observation")),
            )
            # Subject breaks ties: creatinine and eGFR are drawn from the same
            # panel and share a timestamp, so without it their order shuffles
            # between runs and the screen changes shape mid-rehearsal.
            .order_by(m.Fact.reported_at.desc(), m.Fact.subject.asc())
        )
    ).scalars().all()

    labs = [
        LabSummary(
            name=f.subject,
            value=f.value,
            unit=None,  # unit rides in the value string (contract LabSummary)
            observed_at=f.reported_at,
            source_type=f.source_type,
            source_class=f.source_class,
            method=f.method,
            encounter_id=f.encounter_id,
        )
        for f in facts
        if f.fact_type == "lab"
    ]

    blood_pressure = [r for r in (blood_pressure_reading(f) for f in facts) if r is not None]
    blood_pressure.sort(key=lambda r: r.observed_at)

    detail = PatientDetail(
        id=p.id,
        name=p.name,
        age=p.age,
        sex=p.sex,
        mrn=p.mrn,
        birth_date=p.birth_date,
        appointment_time=p.appointment_time,
        conditions=p.conditions or [],
        medications=p.medications or [],
        labs=labs,
        blood_pressure=blood_pressure,
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
