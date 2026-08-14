"""Deepgram live relay (spec §8) — the backend-owned leg of the transcription
pipeline, plus the replay-fixture streamer and the pure helpers the encounter
WS handler builds on.

Pinned architecture (spec §8, do not improvise):
- Browser audio arrives as 16 kHz mono Int16 PCM binary WS frames and is
  relayed verbatim to Deepgram live (encoding=linear16&sample_rate=16000).
- KeepAlive JSON during silence — Deepgram closes idle streams after ~10 s
  (NET-0001); we send every ~5 s when no audio is flowing.
- Reconnect with exponential backoff; raw PCM is header-less so reconnects
  need no container renegotiation. Audio arriving while down is buffered
  (bounded) and flushed on reconnect; overflow → status "degraded".
- On End Visit: send {"type":"CloseStream"} and await Deepgram's trailing
  final results (grace timeout) before the spec §12 pipeline runs.
- Connection status is surfaced via conn.status envelopes — REAL states only
  (spec §2.4): a connection failure is reported as status, never papered over
  with canned transcript text.
- Diarization (diarize=true) is a HINT only; the manual per-utterance
  Doctor/Patient toggle (speaker.correct) is the primary path (spec §8A).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import wave
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Literal
from urllib.parse import urlencode

from app.config import get_settings
from app.schemas.core import Speaker, TranscriptSegment

logger = logging.getLogger(__name__)

# --- Audio math (16 kHz mono Int16 PCM) ------------------------------------

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE  # 32000
CHUNK_SECONDS = 0.1  # 100 ms — REAL-TIME pace; Deepgram stalls on faster input
CHUNK_BYTES = int(BYTES_PER_SECOND * CHUNK_SECONDS)  # 3200

# --- Timing / lifecycle constants -------------------------------------------

KEEPALIVE_INTERVAL_SECONDS = 5.0  # < Deepgram ~10 s idle timeout (NET-0001)
KEEPALIVE_TICK_SECONDS = 1.0
RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0)  # exponential; then "error"
FINALIZE_GRACE_SECONDS = 20.0  # await trailing finals after CloseStream
RECONNECT_BUFFER_MAX_BYTES = BYTES_PER_SECOND * 30  # ~30 s buffered during outage

KEEPALIVE_MESSAGE = json.dumps({"type": "KeepAlive"})
CLOSE_STREAM_MESSAGE = json.dumps({"type": "CloseStream"})

ConnectionState = Literal["connected", "reconnecting", "degraded", "error"]


@dataclass
class RelayTranscript:
    """Normalized transcript event handed to the encounter pipeline."""

    text: str
    is_final: bool
    ts: float  # seconds from session (stream) start
    speaker_hint: int | None = None  # Deepgram diarization is a HINT only (spec §8)


#: Callback signatures the encounter WS handler registers.
TranscriptHandler = Callable[[RelayTranscript], Awaitable[None]]
StatusHandler = Callable[[ConnectionState, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_transcription.py)
# ---------------------------------------------------------------------------


def keepalive_due(
    now: float,
    last_audio_at: float,
    last_keepalive_at: float,
    interval: float = KEEPALIVE_INTERVAL_SECONDS,
) -> bool:
    """True when the stream has been silent for >= interval AND we have not
    already KeepAlive'd within the last interval. Pure — time is a parameter."""
    return (now - last_audio_at) >= interval and (now - last_keepalive_at) >= interval


def dominant_speaker(words: list[dict]) -> int | None:
    """Majority-vote Deepgram speaker int across an alternative's words.

    Returns None when diarization data is absent (hint only, never invented)."""
    votes = [w["speaker"] for w in words if isinstance(w.get("speaker"), int)]
    if not votes:
        return None
    return Counter(votes).most_common(1)[0][0]


class SpeakerMapper:
    """Maps Deepgram diarization ints (hints) to doctor/patient roles.

    First distinct speaker heard = doctor (the clinician opens the visit),
    second distinct = patient. Later ints (diarizer over-segmentation) stick
    with the current speaker. Labels are HINTS — speaker.correct persists the
    clinician's manual corrections over these (spec §8A)."""

    def __init__(self) -> None:
        self._roles: dict[int, Speaker] = {}
        self._last: Speaker = "doctor"

    def role_for(self, hint: int | None) -> Speaker:
        if hint is None:
            return self._last  # no diarization data → stay with current speaker
        role = self._roles.get(hint)
        if role is None:
            if not self._roles:
                role = "doctor"
            elif "patient" not in self._roles.values():
                role = "patient"
            else:
                role = self._last
            self._roles[hint] = role
        self._last = role
        return role


def make_segment_id(encounter_id: str, n: int) -> str:
    """Deterministic per-encounter segment id: seg_<encounter-suffix>_<n>.

    seg_<n> alone would collide across encounters (transcript_segments.id is a
    global primary key), so the id carries the encounter suffix while staying
    deterministic and ordered within one encounter."""
    suffix = encounter_id.removeprefix("enc_") or encounter_id
    return f"seg_{suffix}_{n}"


class SegmentAssembler:
    """Turns RelayTranscript events into contract TranscriptSegment envelopes.

    Finals get seg ids numbered 1..N per encounter; an interim carries the id
    of the NEXT final so the UI can replace interim text in place. Seed
    `final_count` from rows already persisted for the encounter so a resumed
    session never re-issues an existing id."""

    def __init__(self, encounter_id: str, final_count: int = 0) -> None:
        self.encounter_id = encounter_id
        self.final_count = final_count
        self.mapper = SpeakerMapper()

    def next_segment(self, event: RelayTranscript) -> TranscriptSegment:
        role = self.mapper.role_for(event.speaker_hint)
        if event.is_final:
            self.final_count += 1
            n = self.final_count
        else:
            n = self.final_count + 1
        return TranscriptSegment(
            id=make_segment_id(self.encounter_id, n),
            speaker=role,
            text=event.text,
            ts=round(event.ts, 3),
        )


def segment_row_values(encounter_id: str, segment: TranscriptSegment) -> dict:
    """Column values for persisting a FINAL segment as a TranscriptSegment row."""
    return {
        "id": segment.id,
        "encounter_id": encounter_id,
        "speaker": segment.speaker,
        "text": segment.text,
        "ts": segment.ts,
        "is_final": True,
    }


# ---------------------------------------------------------------------------
# Replay source (spec §8 — scripted replay mode, P0)
# ---------------------------------------------------------------------------


class FixtureFormatError(RuntimeError):
    pass


def load_fixture_pcm(wav_path: str | Path) -> bytes:
    """Read the replay fixture WAV, validating the pipeline format."""
    path = Path(wav_path)
    with wave.open(str(path), "rb") as wav:
        if (
            wav.getnchannels() != 1
            or wav.getsampwidth() != BYTES_PER_SAMPLE
            or wav.getframerate() != SAMPLE_RATE
        ):
            raise FixtureFormatError(
                f"Fixture {path} must be 16 kHz mono 16-bit PCM; got "
                f"{wav.getframerate()} Hz / {wav.getnchannels()} ch / "
                f"{wav.getsampwidth() * 8}-bit."
            )
        return wav.readframes(wav.getnframes())


async def iter_pcm_chunks(
    pcm: bytes,
    *,
    chunk_bytes: int = CHUNK_BYTES,
    chunk_seconds: float = CHUNK_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[bytes]:
    """Yield PCM in 100 ms chunks at REAL-TIME pace (0.1 s sleep per chunk).

    Load-bearing: Deepgram's streaming buffer assumes <= real-time input;
    faster-than-realtime on long audio stalls its flush (observed on the full
    238 s fixture at 2x). `sleep` is injectable for tests."""
    for i in range(0, len(pcm), chunk_bytes):
        yield pcm[i : i + chunk_bytes]
        await sleep(chunk_seconds)


async def iter_replay_chunks(wav_path: str | Path) -> AsyncIterator[bytes]:
    """Replay mode: the bundled miller_encounter.wav as paced PCM chunks."""
    pcm = await asyncio.to_thread(load_fixture_pcm, wav_path)
    async for chunk in iter_pcm_chunks(pcm):
        yield chunk


# ---------------------------------------------------------------------------
# The relay
# ---------------------------------------------------------------------------


class DeepgramRelay:
    """One relay per encounter WebSocket.

    Lifecycle: start() → send_audio() xN → finalize() → close().
    All failure modes surface through on_status — never canned output."""

    def __init__(
        self,
        on_transcript: TranscriptHandler,
        on_status: StatusHandler,
        *,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self.on_transcript = on_transcript
        self.on_status = on_status
        self._api_key = settings.deepgram_api_key if api_key is None else api_key
        self._model = model or settings.deepgram_model
        self.state: ConnectionState = "error"  # honest until actually connected
        self.bytes_sent = 0
        self._ws = None
        self._receiver_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._closing = False
        self._ts_offset = 0.0  # keeps ts monotonic across reconnects
        self._last_audio_at = time.monotonic()
        self._last_keepalive_at = time.monotonic()
        self._pending: deque[bytes] = deque()  # audio buffered during an outage
        self._pending_bytes = 0
        self._dropped_bytes = 0

    # -- connection ---------------------------------------------------------

    def build_url(self) -> str:
        params = {
            "encoding": "linear16",
            "sample_rate": str(SAMPLE_RATE),
            "channels": "1",
            "model": self._model,
            "interim_results": "true",
            "smart_format": "true",
            "diarize": "true",  # hint only; manual toggle is primary (spec §8A)
            "endpointing": "300",  # ms of trailing silence closing an utterance
        }
        return "wss://api.deepgram.com/v1/listen?" + urlencode(params)

    async def _open_ws(self):
        import websockets

        headers = {"Authorization": f"Token {self._api_key}"}
        try:
            return await websockets.connect(self.build_url(), additional_headers=headers)
        except TypeError:  # websockets < 12 uses extra_headers
            return await websockets.connect(self.build_url(), extra_headers=headers)

    async def start(self) -> bool:
        """Open the Deepgram live connection; start receive + keepalive loops.

        Returns False (after emitting a real error status) when the key is
        missing or the connect fails — no fake liveness (spec §2.4)."""
        if not self._api_key:
            await self._set_status(
                "error", "DEEPGRAM_API_KEY not configured — transcription unavailable."
            )
            return False
        try:
            self._ws = await self._open_ws()
        except Exception as exc:  # noqa: BLE001 — surfaced as honest status
            await self._set_status(
                "error", f"Deepgram connect failed: {type(exc).__name__}: {exc}"
            )
            return False
        self._receiver_task = asyncio.create_task(self._receive_loop(self._ws))
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        await self._set_status("connected", f"Deepgram live connected (model={self._model}).")
        return True

    # -- audio --------------------------------------------------------------

    async def send_audio(self, pcm: bytes) -> None:
        """Relay one binary frame of 16 kHz mono Int16 PCM upstream.

        During an outage audio is buffered (bounded) and flushed on reconnect
        — raw PCM needs no renegotiation, so this is safe."""
        if self._closing or not pcm:
            return
        self._last_audio_at = time.monotonic()
        if self.state in ("connected", "degraded") and self._ws is not None:
            try:
                await self._ws.send(pcm)
                self.bytes_sent += len(pcm)
                return
            except Exception:  # noqa: BLE001 — dropped mid-send; reconnect below
                self._begin_reconnect("send failed")
        self._buffer_audio(pcm)

    def _buffer_audio(self, pcm: bytes) -> None:
        self._pending.append(pcm)
        self._pending_bytes += len(pcm)
        while self._pending_bytes > RECONNECT_BUFFER_MAX_BYTES:
            dropped = self._pending.popleft()
            self._pending_bytes -= len(dropped)
            self._dropped_bytes += len(dropped)

    async def _flush_pending(self) -> int:
        flushed = 0
        while self._pending:
            chunk = self._pending.popleft()
            self._pending_bytes -= len(chunk)
            await self._ws.send(chunk)
            self.bytes_sent += len(chunk)
            flushed += len(chunk)
        return flushed

    # -- keepalive (NET-0001) -----------------------------------------------

    async def _keepalive_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(KEEPALIVE_TICK_SECONDS)
            now = time.monotonic()
            if not keepalive_due(now, self._last_audio_at, self._last_keepalive_at):
                continue
            if self.state in ("connected", "degraded") and self._ws is not None:
                try:
                    await self._ws.send(KEEPALIVE_MESSAGE)
                    self._last_keepalive_at = now
                except Exception:  # noqa: BLE001 — receiver loop handles the drop
                    logger.debug("KeepAlive send failed; reconnect will pick it up")

    # -- receive ------------------------------------------------------------

    async def _receive_loop(self, ws) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if msg.get("type") == "Results":
                    await self._handle_results(msg)
                # Metadata / UtteranceEnd / SpeechStarted are not needed here.
        except Exception as exc:  # noqa: BLE001
            if not self._closing:
                logger.warning("Deepgram receive loop ended: %s: %s", type(exc).__name__, exc)
        finally:
            if not self._closing and ws is self._ws:
                self._begin_reconnect("connection closed")

    async def _handle_results(self, msg: dict) -> None:
        alternatives = (msg.get("channel") or {}).get("alternatives") or []
        alt = alternatives[0] if alternatives else {}
        text = (alt.get("transcript") or "").strip()
        if not text:
            return
        event = RelayTranscript(
            text=text,
            is_final=bool(msg.get("is_final")),
            ts=self._ts_offset + float(msg.get("start") or 0.0),
            speaker_hint=dominant_speaker(alt.get("words") or []),
        )
        try:
            await self.on_transcript(event)
        except Exception:  # noqa: BLE001 — one bad handler must not kill the stream
            logger.exception("on_transcript handler failed")

    # -- reconnect ----------------------------------------------------------

    def _begin_reconnect(self, reason: str) -> None:
        if self._closing or (self._reconnect_task and not self._reconnect_task.done()):
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop(reason))

    async def _reconnect_loop(self, reason: str) -> None:
        # New Deepgram stream restarts its clock at 0; offset keeps our ts
        # continuous. Dropped audio still represents elapsed session time.
        self._ts_offset = (self.bytes_sent + self._dropped_bytes) / BYTES_PER_SECOND
        self._ws = None
        for attempt, delay in enumerate(RECONNECT_BACKOFF_SECONDS, start=1):
            if self._closing:
                return
            await self._set_status(
                "reconnecting",
                f"Deepgram connection lost ({reason}); retry "
                f"{attempt}/{len(RECONNECT_BACKOFF_SECONDS)} in {delay:g}s.",
            )
            await asyncio.sleep(delay)
            if self._closing:
                return
            try:
                ws = await self._open_ws()
            except Exception as exc:  # noqa: BLE001 — keep backing off
                logger.warning("Deepgram reconnect attempt %d failed: %s", attempt, exc)
                continue
            self._ws = ws
            self._receiver_task = asyncio.create_task(self._receive_loop(ws))
            dropped_before = self._dropped_bytes
            try:
                await self._flush_pending()
            except Exception:  # noqa: BLE001 — flush failed; loop again via receiver
                logger.warning("Flush of buffered audio failed after reconnect")
            if dropped_before:
                await self._set_status(
                    "degraded",
                    f"Deepgram reconnected; ~{dropped_before // BYTES_PER_SECOND}s of "
                    "audio was dropped during the outage.",
                )
            else:
                await self._set_status("connected", f"Deepgram reconnected (attempt {attempt}).")
            return
        await self._set_status("error", "Deepgram reconnection attempts exhausted.")

    # -- finalize / close ----------------------------------------------------

    async def finalize(self, grace_seconds: float = FINALIZE_GRACE_SECONDS) -> None:
        """Send CloseStream and await trailing finals (grace timeout).

        Trailing finals flow through on_transcript exactly like live ones, so
        the caller's persistence path handles stragglers before this returns."""
        if self._closing:
            return
        self._closing = True
        self._cancel(self._keepalive_task)
        self._cancel(self._reconnect_task)
        ws, receiver = self._ws, self._receiver_task
        if ws is not None:
            try:
                await ws.send(CLOSE_STREAM_MESSAGE)
            except Exception:  # noqa: BLE001 — already down; nothing to flush
                logger.debug("CloseStream send failed (connection already down)")
        if receiver is not None and not receiver.done():
            try:
                # Deepgram flushes remaining Results then closes the socket,
                # which ends the receive loop naturally.
                await asyncio.wait_for(asyncio.shield(receiver), timeout=grace_seconds)
            except asyncio.TimeoutError:
                logger.warning("Deepgram finalize grace window elapsed; closing anyway")
                receiver.cancel()
            except Exception:  # noqa: BLE001
                pass
        await self._teardown()

    async def close(self) -> None:
        """Tear down connection and background tasks. Safe in any state."""
        self._closing = True
        self._cancel(self._keepalive_task)
        self._cancel(self._reconnect_task)
        self._cancel(self._receiver_task)
        await self._teardown()

    async def _teardown(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        if task is not None and not task.done():
            task.cancel()

    # -- status --------------------------------------------------------------

    async def _set_status(self, state: ConnectionState, detail: str) -> None:
        self.state = state
        logger.info("Deepgram relay status: %s — %s", state, detail)
        try:
            await self.on_status(state, detail)
        except Exception:  # noqa: BLE001 — status fan-out must not kill the relay
            logger.exception("on_status handler failed")
