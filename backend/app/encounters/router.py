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

from app.ai.care_plan import generate_care_plan
from app.ai.encounter_summary import generate_encounter_summary
from app.ai.executor import get_executor
from app.ai.extraction import FactView
from app.ai.pipeline import DatabaseStore, EncounterPipeline
from app.evals.evaluators import run_online_evals
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
    StateFactMessage,
    Suggestion,
    SuggestionActiveMessage,
    SuggestionDismissMessage,
    SuggestionRemoveMessage,
    TranscriptFinalMessage,
    TranscriptInterimMessage,
    TranscriptSegment,
)

REPLAY_FIXTURE_RELPATH = Path("audio/miller_encounter.wav")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/encounters", tags=["encounters"])


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _fact_view(f: m.Fact) -> FactView:
    return FactView(
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


@router.get("/latest")
async def get_latest_encounter(
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    """Most recent encounter — the AI Operations default. Remembered ids go
    stale after `make reset-runtime` deletes encounters; the ops view falls
    back to this instead of pointing at a ghost (honest 404 only when the
    database truly has no encounters)."""
    encounter = (
        await session.execute(
            select(m.Encounter).order_by(m.Encounter.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No encounters exist yet")
    return {"encounter": _encounter_payload(encounter)}


@router.get("/{encounter_id}")
async def get_encounter(
    encounter_id: str,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    encounter = await session.get(m.Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Encounter not found")
    # Chart facts (encounter_id NULL) populate Column B's "from chart" section;
    # THIS encounter's facts are NEW TODAY (spec §8B). Other encounters' facts
    # are excluded — rehearsal runs must not leak into a fresh encounter's view.
    facts = (
        await session.execute(
            select(m.Fact).where(
                m.Fact.patient_id == encounter.patient_id,
                (m.Fact.encounter_id == encounter_id) | (m.Fact.encounter_id.is_(None)),
            )
        )
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

    if not actions:
        # Spec §12 step 5: REAL care-plan generation. Failures surface honestly
        # (spec §2.4/§31) — the frontend shows a generation-failed state + retry.
        fact_rows = (
            await session.execute(
                select(m.Fact).where(
                    m.Fact.patient_id == encounter.patient_id,
                    (m.Fact.encounter_id == encounter_id) | (m.Fact.encounter_id.is_(None)),
                )
            )
        ).scalars().all()
        segments = (
            await session.execute(
                select(m.TranscriptSegment)
                .where(m.TranscriptSegment.encounter_id == encounter_id)
                .order_by(m.TranscriptSegment.ts)
            )
        ).scalars().all()
        transcript_text = "\n".join(f"{s.speaker}: {s.text}" for s in segments)
        try:
            candidates = await generate_care_plan(
                encounter_id=encounter_id,
                patient_id=encounter.patient_id,
                facts=[_fact_view(f) for f in fact_rows],
                transcript_text=transcript_text,
                trace_id=encounter.trace_id,
            )
        except Exception as exc:  # noqa: BLE001 — honest failure, never canned
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=f"Care-plan generation failed: {type(exc).__name__}: {exc}",
            ) from None
        for cand in candidates:
            session.add(
                m.CarePlanAction(
                    id=_new_id("act"),
                    care_plan_id=care_plan.id,
                    category=cand.category,
                    title=cand.title,
                    description=cand.description,
                    rationale=cand.rationale,
                    patient_facts_used=cand.patient_facts_used,
                    evidence_refs=cand.evidence_refs,
                    risk_level=cand.risk_level,
                    permission=cand.permission,
                    status="pending",
                )
            )
        care_plan.status = "in_review"

        # Spec §12 step 4: encounter summary (pre-decision). Non-fatal on
        # failure — summary is contextual, the care flow continues (§2.4).
        if encounter.summary is None:
            try:
                generated = await generate_encounter_summary(
                    encounter_id=encounter_id,
                    patient_id=encounter.patient_id,
                    facts=[_fact_view(f) for f in fact_rows],
                    transcript_text=transcript_text,
                    trace_id=encounter.trace_id,
                )
                encounter.summary = generated.text
            except Exception as exc:  # noqa: BLE001
                logger.warning("Encounter summary generation failed for %s: %s", encounter_id, exc)
        await session.commit()

        # Spec §12 step 6: initial online evaluators — in the BACKGROUND on
        # the pipeline loop. They are observability, not response content;
        # keeping them in-request pushed End Visit past the proxy timeout
        # (user-observed 500s). Failures degrade observability only.
        def _log_eval_result(fut) -> None:
            exc = fut.exception()
            if exc is not None:
                logger.warning("Background online evals failed for %s: %s", encounter_id, exc)

        get_executor().submit(run_online_evals(encounter_id)).add_done_callback(_log_eval_result)
        actions = (
            await session.execute(
                select(m.CarePlanAction).where(m.CarePlanAction.care_plan_id == care_plan.id)
            )
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
    patient_id: str | None = None
    trace_id: str | None = None
    async with session_factory() as session:
        encounter = await session.get(m.Encounter, encounter_id)
        if encounter is not None:
            encounter.status = "live"
            encounter.mode = mode
            patient_id = encounter.patient_id
            trace_id = encounter.trace_id
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

    # AI loop wiring (spec §9): finalized segments feed the extraction
    # pipeline ON ITS OWN EVENT LOOP (app.ai.executor) — realtime audio can
    # never starve model/DB work. Callbacks run on the pipeline loop and
    # marshal WS sends back to THIS loop thread-safely.
    main_loop = asyncio.get_running_loop()
    executor = get_executor()

    def on_fact(fact, change) -> None:
        asyncio.run_coroutine_threadsafe(
            send_safe(StateFactMessage(fact=fact, change=change)), main_loop
        )

    def on_suggestion(sug) -> None:
        asyncio.run_coroutine_threadsafe(
            send_safe(SuggestionActiveMessage(suggestion=sug)), main_loop
        )

    def on_suggestion_remove(suggestion_id: str) -> None:
        asyncio.run_coroutine_threadsafe(
            send_safe(SuggestionRemoveMessage(suggestion_id=suggestion_id)), main_loop
        )

    pipeline: EncounterPipeline | None = None
    if patient_id is not None:
        pipeline = EncounterPipeline(
            encounter_id=encounter_id,
            patient_id=patient_id,
            trace_id=trace_id,
            store=DatabaseStore(),  # lazy factory — binds to the pipeline loop
            on_fact=on_fact,
            on_suggestion=on_suggestion,
            on_suggestion_remove=on_suggestion_remove,
        )

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
        if pipeline is not None:
            # Fire-and-forget onto the pipeline loop; feeding only buffers +
            # arms the debounce there, so cycles run fully off this loop.
            executor.submit(pipeline.feed_final_segment(segment))

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
                if pipeline is not None:
                    # Final extraction pass over any remaining buffer (spec §12
                    # step 2) — runs on the pipeline loop, awaited from here.
                    await executor.run(pipeline.flush())
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
        if pipeline is not None:
            with contextlib.suppress(Exception):
                await executor.run(pipeline.close())
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
