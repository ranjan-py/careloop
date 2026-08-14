"""Encounter routes — create, read, end, and the live-stream WebSocket.

The WS endpoint implements the CONTRACTS.md handshake (one-time ticket via
session.start) and relays audio through the REAL Deepgram live connection
(app.transcription.deepgram_relay). mode=live relays browser PCM frames;
mode=replay streams the bundled fixture through the SAME relay at real-time
pace (spec §8). Connection state is surfaced honestly via conn.status —
failures are reported, never papered over (spec §2.4).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import require_clinician
from app.config import get_settings
from app.db import models as m
from app.db.session import get_session, get_session_factory
from app.encounters.tickets import issue_ticket, redeem_ticket
from app.transcription.deepgram_relay import (
    ConnectionState,
    DeepgramRelay,
    FixtureFormatError,
    RelayTranscript,
    SegmentAssembler,
    iter_replay_chunks,
    segment_row_values,
)
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
    TranscriptFinalMessage,
    TranscriptInterimMessage,
    TranscriptSegment,
)

REPLAY_FIXTURE_RELPATH = Path("audio/miller_encounter.wav")

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

    mode = first.mode
    session_factory = get_session_factory()
    async with session_factory() as session:
        encounter = await session.get(m.Encounter, encounter_id)
        if encounter is not None:
            encounter.status = "live"
            encounter.mode = mode
            await session.commit()
        # Deterministic seg_<n> ids continue after any rows already persisted
        # for this encounter (page-refresh re-hydration must never collide).
        existing_finals = (
            await session.execute(
                select(func.count())
                .select_from(m.TranscriptSegment)
                .where(m.TranscriptSegment.encounter_id == encounter_id)
            )
        ).scalar_one()

    # Envelope sends can originate from the relay's receiver/status tasks and
    # the main loop concurrently; serialize them and tolerate a gone client.
    send_lock = asyncio.Lock()
    socket_open = True

    async def send_safe(message: BaseModel) -> None:
        nonlocal socket_open
        if not socket_open:
            return
        async with send_lock:
            try:
                await ws.send_text(message.model_dump_json())
            except Exception:  # noqa: BLE001 — client gone; disconnect path logs
                socket_open = False

    assembler = SegmentAssembler(encounter_id, final_count=existing_finals)
    finals_persisted = 0

    async def on_transcript(event: RelayTranscript) -> None:
        """Interims go to the UI only; FINALS persist first, then stream out
        (a page refresh must re-hydrate everything the client saw, spec §8)."""
        nonlocal finals_persisted
        segment = assembler.next_segment(event)
        if not event.is_final:
            await send_safe(TranscriptInterimMessage(segment=segment))
            return
        async with session_factory() as session:
            session.add(m.TranscriptSegment(**segment_row_values(encounter_id, segment)))
            await session.commit()
        finals_persisted += 1
        await send_safe(TranscriptFinalMessage(segment=segment))

    async def on_status(state: ConnectionState, detail: str) -> None:
        await send_safe(ConnStatusMessage(deepgram=state, detail=detail))

    relay = DeepgramRelay(on_transcript=on_transcript, on_status=on_status)
    # start() emits a REAL conn.status either way — "connected", or "error"
    # with the actual failure; no fake liveness (spec §2.4).
    relay_started = await relay.start()

    async def run_replay() -> None:
        """Replay mode: stream the fixture through the SAME relay at real-time
        pace; client audio frames are ignored (spec §8 scripted replay)."""
        wav_path = Path(get_settings().data_dir) / REPLAY_FIXTURE_RELPATH
        try:
            async for chunk in iter_replay_chunks(wav_path):
                await relay.send_audio(chunk)
        except asyncio.CancelledError:
            raise
        except (FileNotFoundError, FixtureFormatError, OSError) as exc:
            await send_safe(
                ErrorMessage(
                    scope="internal",
                    message=f"Replay fixture unavailable: {exc}",
                    recoverable=False,
                )
            )
        else:
            logger.info("Replay fixture fully streamed for encounter %s", encounter_id)

    replay_task: asyncio.Task | None = None
    if mode == "replay" and relay_started:
        replay_task = asyncio.create_task(run_replay())

    audio_bytes_received = 0
    ignored_replay_audio = 0

    async def shutdown_streaming() -> None:
        if replay_task is not None and not replay_task.done():
            replay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await replay_task

    try:
        while True:
            frame = await ws.receive()
            if frame.get("type") == "websocket.disconnect":
                break
            if frame.get("bytes") is not None:
                # Binary frames: 16 kHz mono Int16 PCM from the browser.
                chunk = frame["bytes"]
                audio_bytes_received += len(chunk)
                if mode == "live":
                    await relay.send_audio(chunk)
                else:
                    ignored_replay_audio += len(chunk)  # replay feeds the relay itself
                continue
            text = frame.get("text")
            if text is None:
                continue
            try:
                message = ClientMessageAdapter.validate_python(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                await send_safe(ErrorMessage(scope="internal", message=f"Bad message: {exc}", recoverable=True))
                continue

            if isinstance(message, SpeakerCorrectMessage):
                # Manual Doctor/Patient toggle is the PRIMARY diarization path
                # (spec §8A); diarize hints only pre-label segments.
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
                await send_safe(SessionFinalizingMessage())
                # Flush Deepgram: CloseStream, await trailing finals (grace
                # window) — stragglers persist through on_transcript before
                # finalize() returns (spec §8 End Visit).
                await shutdown_streaming()
                await relay.finalize()
                async with session_factory() as session:
                    encounter = await session.get(m.Encounter, encounter_id)
                    if encounter is not None:
                        encounter.status = "finalizing"
                        await session.commit()
                await send_safe(SessionFinalizedMessage(encounter_id=encounter_id))
                with contextlib.suppress(Exception):
                    await ws.close()
                socket_open = False
                break
            elif isinstance(message, SessionStartMessage):
                await send_safe(ErrorMessage(scope="internal", message="Session already started", recoverable=True))
    except WebSocketDisconnect:
        pass
    finally:
        socket_open = False
        await shutdown_streaming()
        await relay.close()
        logger.info(
            "WS session closed for encounter %s: mode=%s client_audio_bytes=%d "
            "relay_bytes_sent=%d finals_persisted=%d ignored_replay_audio_bytes=%d",
            encounter_id,
            mode,
            audio_bytes_received,
            relay.bytes_sent,
            finals_persisted,
            ignored_replay_audio,
        )
