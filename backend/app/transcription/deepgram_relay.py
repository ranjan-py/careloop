"""Deepgram live-relay skeleton (spec §8) — structure and interfaces now;
live wiring lands in the transcription milestone.

Pinned architecture (do not improvise):
- The backend owns the Deepgram leg of the pipeline. Browser audio arrives as
  16 kHz mono Int16 PCM binary WS frames and is relayed to Deepgram live
  (encoding=linear16&sample_rate=16000).
- KeepAlive JSON messages during silence — Deepgram closes idle streams after
  ~10 s (NET-0001).
- Reconnect with backoff; raw PCM is header-less so reconnects need no
  container renegotiation.
- On End Visit: send CloseStream/Finalize and await final results before the
  spec §12 pipeline runs.
- Connection status is surfaced to the UI via conn.status envelopes — real
  states only, never a pretend "connected" (spec §2.4).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

from app.config import get_settings

logger = logging.getLogger(__name__)

KEEPALIVE_INTERVAL_SECONDS = 5.0  # < Deepgram ~10 s idle timeout (NET-0001)
RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0)  # then give up → status "error"

ConnectionState = Literal["connected", "reconnecting", "degraded", "error"]


@dataclass
class RelayTranscript:
    """Normalized transcript event handed to the encounter pipeline."""

    text: str
    is_final: bool
    ts: float  # seconds from stream start
    speaker_hint: int | None = None  # Deepgram diarization is a HINT only (spec §8)


#: Callback signatures the encounter WS handler registers.
TranscriptHandler = Callable[[RelayTranscript], Awaitable[None]]
StatusHandler = Callable[[ConnectionState, str], Awaitable[None]]


@dataclass
class DeepgramRelay:
    """One relay per encounter WebSocket.

    Lifecycle: start() → send_audio() xN → finish() → close().
    NOT YET WIRED: connect/receive loops raise NotImplementedError so any
    accidental use fails loudly instead of faking transcription (spec §2.1).
    """

    on_transcript: TranscriptHandler
    on_status: StatusHandler
    state: ConnectionState = "error"
    _keepalive_task: asyncio.Task | None = field(default=None, repr=False)
    _reconnect_attempts: int = 0

    def build_connection_params(self) -> dict:
        """Deepgram live options — kept in one place for the wiring milestone."""
        settings = get_settings()
        return {
            "model": settings.deepgram_model,
            "encoding": "linear16",
            "sample_rate": 16000,
            "channels": 1,
            "interim_results": True,
            "diarize": True,  # hint only; manual per-utterance toggle is primary
            "smart_format": True,
        }

    async def start(self) -> None:
        """Open the Deepgram live connection and start receive + keepalive loops."""
        raise NotImplementedError(
            "Deepgram live wiring lands in the transcription milestone — "
            "BLOCKED, real integration not verified."
        )

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """Relay one binary frame of 16 kHz mono Int16 PCM upstream."""
        raise NotImplementedError("Deepgram live wiring lands in the transcription milestone.")

    async def finish(self) -> None:
        """Send CloseStream/Finalize and await Deepgram's final results (spec §8)."""
        raise NotImplementedError("Deepgram live wiring lands in the transcription milestone.")

    async def close(self) -> None:
        """Tear down connection and background tasks. Safe to call in any state."""
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None

    # -- scaffolding the wiring milestone fills in -------------------------

    async def _keepalive_loop(self) -> None:
        """Send Deepgram KeepAlive JSON during silence every KEEPALIVE_INTERVAL_SECONDS."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
            # wiring milestone: await self._dg_connection.send('{"type":"KeepAlive"}')
            logger.debug("KeepAlive tick (not wired)")

    async def _reconnect_with_backoff(self) -> None:
        """On drop: emit 'reconnecting', retry per RECONNECT_BACKOFF_SECONDS,
        then emit 'error' if exhausted. Raw PCM needs no renegotiation."""
        for delay in RECONNECT_BACKOFF_SECONDS:
            self.state = "reconnecting"
            await self.on_status("reconnecting", f"Retrying Deepgram in {delay:g}s")
            await asyncio.sleep(delay)
            # wiring milestone: attempt reconnect; on success -> "connected", return
        self.state = "error"
        await self.on_status("error", "Deepgram reconnection attempts exhausted")


async def stream_replay_fixture(relay: DeepgramRelay, wav_path: str) -> None:
    """Replay mode (spec §8): stream data/audio/miller_encounter.wav through the
    SAME real Deepgram path at real-time pace. Wiring milestone implements the
    paced read loop; the fixture is TTS-generated (synthetic — labeled)."""
    raise NotImplementedError("Replay streaming lands with the transcription milestone.")
