"""Encounter routes — create, read, end, and the live-stream WebSocket.

The WS endpoint currently implements the CONTRACTS.md handshake (one-time
ticket via session.start) and envelope plumbing; the real Deepgram relay
(app.transcription) is wired in a later milestone and the socket honestly
reports deepgram status "error" until then (spec §2.4 — never fake liveness).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import require_clinician
from app.db import models as m
from app.db.session import get_session, get_session_factory
from app.encounters.tickets import issue_ticket, redeem_ticket
from app.schemas.core import (
    CarePlanAction,
    ClientMessageAdapter,
    Clinician,
    ConnStatusMessage,
    ErrorMessage,
    Fact,
    SessionEndMessage,
    SessionFinalizedMessage,
    SessionFinalizingMessage,
    SessionStartMessage,
    SpeakerCorrectMessage,
    Suggestion,
    SuggestionDismissMessage,
    TranscriptSegment,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/encounters", tags=["encounters"])


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _encounter_payload(e: m.Encounter, stream_ticket: str | None = None) -> dict:
    payload = {
        "id": e.id,
        "patient_id": e.patient_id,
        "clinician_id": e.clinician_id,
        "status": e.status,
        "mode": e.mode,
        "trace_id": e.trace_id,
        "summary": e.summary,
        "started_at": e.started_at.isoformat() if e.started_at else None,
        "ended_at": e.ended_at.isoformat() if e.ended_at else None,
    }
    if stream_ticket is not None:
        # One-time WS ticket rides inside the {encounter} response shape
        # (CONTRACTS.md pins the shape to {encounter}; see WS protocol note).
        payload["stream_ticket"] = stream_ticket
    return payload


class CreateEncounterRequest(BaseModel):
    patient_id: str


@router.post("")
async def create_encounter(
    body: CreateEncounterRequest,
    session: AsyncSession = Depends(get_session),
    clinician: Clinician = Depends(require_clinician),
) -> dict:
    patient = await session.get(m.Patient, body.patient_id)
    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Patient not found")
    encounter = m.Encounter(
        id=_new_id("enc"),
        patient_id=body.patient_id,
        clinician_id=clinician.id,
        status="created",
        # Trace-ID minted at encounter start and persisted (spec §20); all
        # later spans attach to this stored id.
        trace_id=uuid.uuid4().hex,
        started_at=datetime.now(timezone.utc),
    )
    session.add(encounter)
    await session.commit()
    ticket = issue_ticket(encounter.id)
    return {"encounter": _encounter_payload(encounter, stream_ticket=ticket)}


@router.get("/{encounter_id}")
async def get_encounter(
    encounter_id: str,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    encounter = await session.get(m.Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Encounter not found")
    facts = (
        await session.execute(select(m.Fact).where(m.Fact.encounter_id == encounter_id))
    ).scalars().all()
    suggestions = (
        await session.execute(select(m.Suggestion).where(m.Suggestion.encounter_id == encounter_id))
    ).scalars().all()
    segments = (
        await session.execute(
            select(m.TranscriptSegment)
            .where(m.TranscriptSegment.encounter_id == encounter_id)
            .order_by(m.TranscriptSegment.ts)
        )
    ).scalars().all()
    return {
        "encounter": _encounter_payload(encounter),
        "facts": [Fact.model_validate(f).model_dump(mode="json") for f in facts],
        "suggestions": [Suggestion.model_validate(s).model_dump(mode="json") for s in suggestions],
        "transcript": [TranscriptSegment.model_validate(t).model_dump(mode="json") for t in segments],
    }


@router.post("/{encounter_id}/end")
async def end_encounter(
    encounter_id: str,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    """End Visit → spec §12 pipeline.

    Skeleton scope: finalize the encounter row and ensure a draft care plan
    exists. Final extraction / summary / plan generation / online evals are
    the AI pipeline milestone — BLOCKED until real OpenAI wiring, not faked.
    """
    encounter = await session.get(m.Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Encounter not found")
    # Idempotent: re-ending a finalized encounter returns existing state
    # (frontend calls end from both the care-plan and summary pages).
    if encounter.status != "finalized":
        encounter.status = "finalized"
        encounter.ended_at = datetime.now(timezone.utc)

    care_plan = (
        await session.execute(select(m.CarePlan).where(m.CarePlan.encounter_id == encounter_id))
    ).scalar_one_or_none()
    if care_plan is None:
        care_plan = m.CarePlan(
            id=_new_id("cp"),
            encounter_id=encounter_id,
            patient_id=encounter.patient_id,
            status="draft",
        )
        session.add(care_plan)
    await session.commit()

    actions = (
        await session.execute(select(m.CarePlanAction).where(m.CarePlanAction.care_plan_id == care_plan.id))
    ).scalars().all()
    return {
        "encounter": _encounter_payload(encounter),
        "care_plan": {
            "id": care_plan.id,
            "encounter_id": care_plan.encounter_id,
            "patient_id": care_plan.patient_id,
            "status": care_plan.status,
            "actions": [CarePlanAction.model_validate(a).model_dump(mode="json") for a in actions],
        },
    }


# ---------------------------------------------------------------------------
# WebSocket — /api/encounters/{id}/stream
# ---------------------------------------------------------------------------

HANDSHAKE_TIMEOUT_SECONDS = 15.0
WS_POLICY_VIOLATION = 1008


async def _send(ws: WebSocket, message: BaseModel) -> None:
    await ws.send_text(message.model_dump_json())


@router.websocket("/{encounter_id}/stream")
async def encounter_stream(ws: WebSocket, encounter_id: str) -> None:
    await ws.accept()

    # --- Handshake: first text frame must be session.start with a valid ticket
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=HANDSHAKE_TIMEOUT_SECONDS)
        first = ClientMessageAdapter.validate_python(json.loads(raw))
    except (asyncio.TimeoutError, json.JSONDecodeError, ValidationError):
        await _send(ws, ErrorMessage(scope="internal", message="Expected session.start handshake", recoverable=False))
        await ws.close(code=WS_POLICY_VIOLATION)
        return
    except WebSocketDisconnect:
        return

    if not isinstance(first, SessionStartMessage) or not redeem_ticket(first.ticket, encounter_id):
        await _send(ws, ErrorMessage(scope="internal", message="Invalid or expired session ticket", recoverable=False))
        await ws.close(code=WS_POLICY_VIOLATION)
        return

    session_factory = get_session_factory()
    async with session_factory() as session:
        encounter = await session.get(m.Encounter, encounter_id)
        if encounter is not None:
            encounter.status = "live"
            encounter.mode = first.mode
            await session.commit()

    # Honest connection status: the Deepgram relay is not wired in this
    # skeleton, so the socket reports error rather than pretending liveness.
    await _send(
        ws,
        ConnStatusMessage(
            deepgram="error",
            detail="Deepgram relay not wired yet (backend skeleton); transcript streaming unavailable.",
        ),
    )

    audio_bytes_received = 0
    try:
        while True:
            frame = await ws.receive()
            if frame.get("type") == "websocket.disconnect":
                break
            if frame.get("bytes") is not None:
                # Binary frames: 16 kHz mono Int16 PCM. Counted, not yet relayed.
                audio_bytes_received += len(frame["bytes"])
                continue
            text = frame.get("text")
            if text is None:
                continue
            try:
                message = ClientMessageAdapter.validate_python(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                await _send(ws, ErrorMessage(scope="internal", message=f"Bad message: {exc}", recoverable=True))
                continue

            if isinstance(message, SpeakerCorrectMessage):
                async with session_factory() as session:
                    segment = await session.get(m.TranscriptSegment, message.segment_id)
                    if segment is not None and segment.encounter_id == encounter_id:
                        segment.speaker = message.speaker
                        await session.commit()
            elif isinstance(message, SuggestionDismissMessage):
                # Dismissals are persisted for analytics/evals (spec §8C).
                async with session_factory() as session:
                    suggestion = await session.get(m.Suggestion, message.suggestion_id)
                    if suggestion is not None and suggestion.encounter_id == encounter_id:
                        suggestion.status = "dismissed"
                        await session.commit()
            elif isinstance(message, SessionEndMessage):
                await _send(ws, SessionFinalizingMessage())
                async with session_factory() as session:
                    encounter = await session.get(m.Encounter, encounter_id)
                    if encounter is not None:
                        encounter.status = "finalizing"
                        await session.commit()
                await _send(ws, SessionFinalizedMessage(encounter_id=encounter_id))
                await ws.close()
                break
            elif isinstance(message, SessionStartMessage):
                await _send(ws, ErrorMessage(scope="internal", message="Session already started", recoverable=True))
    except WebSocketDisconnect:
        logger.info("WS disconnected for encounter %s (%d audio bytes received)", encounter_id, audio_bytes_received)
