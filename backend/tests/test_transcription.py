"""Unit tests for the Deepgram relay building blocks (spec §8).

Deepgram itself is mocked/absent here per spec §2.3 — the REAL streaming path
is exercised by `python -m app.smoke --only deepgram` and
`scripts/verify_replay.py` against the running stack. These tests cover the
deterministic logic: chunk pacing math, speaker mapping, deterministic segment
ids, KeepAlive triggering (mocked time), and final-segment persistence mapping.
"""

from __future__ import annotations

import pytest

from app.transcription.deepgram_relay import (
    BYTES_PER_SECOND,
    CHUNK_BYTES,
    CHUNK_SECONDS,
    KEEPALIVE_INTERVAL_SECONDS,
    RelayTranscript,
    SegmentAssembler,
    SpeakerMapper,
    dominant_speaker,
    iter_pcm_chunks,
    keepalive_due,
    make_segment_id,
    segment_row_values,
)
from app.schemas.core import (
    ServerMessageAdapter,
    TranscriptFinalMessage,
    TranscriptInterimMessage,
)

# ---------------------------------------------------------------------------
# Chunk pacing math — replay must stream at REAL-TIME pace (load-bearing:
# Deepgram stalls on faster-than-realtime input over long audio).
# ---------------------------------------------------------------------------


class TestChunkPacing:
    def test_chunk_is_100ms_of_16k_mono_int16(self):
        assert BYTES_PER_SECOND == 16000 * 2  # 16 kHz mono Int16
        assert CHUNK_BYTES == 3200
        assert CHUNK_SECONDS == pytest.approx(0.1)
        # A chunk's audio duration equals the sleep between sends: real time.
        assert CHUNK_BYTES / BYTES_PER_SECOND == pytest.approx(CHUNK_SECONDS)

    def test_fixture_chunk_count_math(self):
        # 238 s fixture → 2380 chunks of 100 ms, ~238 s of pacing sleeps.
        fixture_bytes = 238 * BYTES_PER_SECOND
        n_chunks = -(-fixture_bytes // CHUNK_BYTES)  # ceil
        assert n_chunks == 2380
        assert n_chunks * CHUNK_SECONDS == pytest.approx(238.0)

    async def test_generator_yields_100ms_chunks_with_real_time_sleeps(self):
        pcm = bytes(range(256)) * 39  # 9984 bytes: 3 full chunks + 384 remainder
        delays: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)

        chunks = [c async for c in iter_pcm_chunks(pcm, sleep=fake_sleep)]

        assert [len(c) for c in chunks] == [3200, 3200, 3200, 384]
        assert b"".join(chunks) == pcm  # byte-exact, ordered relay
        assert delays == [pytest.approx(0.1)] * 4  # one real-time sleep per chunk

    async def test_generator_handles_empty_audio(self):
        async def fake_sleep(seconds: float) -> None:  # pragma: no cover
            raise AssertionError("no sleep expected for empty audio")

        assert [c async for c in iter_pcm_chunks(b"", sleep=fake_sleep)] == []


# ---------------------------------------------------------------------------
# Speaker mapping — Deepgram diarization ints are HINTS; first speaker heard
# maps to doctor (spec §8A; manual speaker.correct remains the primary path).
# ---------------------------------------------------------------------------


class TestSpeakerMapper:
    def test_first_speaker_is_doctor_second_is_patient(self):
        mapper = SpeakerMapper()
        assert mapper.role_for(1) == "doctor"  # first int seen, whatever value
        assert mapper.role_for(0) == "patient"
        # Stable on repeats, in any order.
        assert mapper.role_for(1) == "doctor"
        assert mapper.role_for(0) == "patient"

    def test_missing_hint_sticks_with_current_speaker(self):
        mapper = SpeakerMapper()
        assert mapper.role_for(None) == "doctor"  # before any hint: doctor opens
        mapper.role_for(0)
        mapper.role_for(1)  # now speaking: patient
        assert mapper.role_for(None) == "patient"

    def test_oversegmented_third_speaker_does_not_crash_roles(self):
        mapper = SpeakerMapper()
        mapper.role_for(0)
        mapper.role_for(1)
        assert mapper.role_for(2) in ("doctor", "patient")  # hint only, stays valid

    def test_dominant_speaker_majority_vote(self):
        words = [{"speaker": 0}, {"speaker": 1}, {"speaker": 1}, {"word": "hi"}]
        assert dominant_speaker(words) == 1

    def test_dominant_speaker_none_without_diarization(self):
        assert dominant_speaker([]) is None
        assert dominant_speaker([{"word": "hi"}]) is None  # never invented


# ---------------------------------------------------------------------------
# Deterministic segment ids
# ---------------------------------------------------------------------------


class TestSegmentIds:
    def test_deterministic_per_encounter(self):
        assert make_segment_id("enc_abc123", 1) == make_segment_id("enc_abc123", 1)
        assert make_segment_id("enc_abc123", 1) == "seg_abc123_1"
        assert make_segment_id("enc_abc123", 2) == "seg_abc123_2"

    def test_scoped_by_encounter_no_cross_encounter_collisions(self):
        # transcript_segments.id is a global primary key — seg_<n> alone would
        # collide across encounters, so ids carry the encounter suffix.
        assert make_segment_id("enc_a", 1) != make_segment_id("enc_b", 1)

    def test_finals_number_sequentially_and_interim_previews_next_id(self):
        assembler = SegmentAssembler("enc_abc123")
        interim = assembler.next_segment(RelayTranscript(text="hel", is_final=False, ts=0.5))
        final1 = assembler.next_segment(RelayTranscript(text="hello", is_final=True, ts=0.5))
        final2 = assembler.next_segment(RelayTranscript(text="again", is_final=True, ts=2.0))
        # The interim carries the id its final will land on → UI replaces in place.
        assert interim.id == final1.id == "seg_abc123_1"
        assert final2.id == "seg_abc123_2"

    def test_counter_seeds_from_already_persisted_rows(self):
        # Page-refresh / resumed session: ids continue after existing rows.
        assembler = SegmentAssembler("enc_abc123", final_count=7)
        final = assembler.next_segment(RelayTranscript(text="more", is_final=True, ts=80.0))
        assert final.id == "seg_abc123_8"


# ---------------------------------------------------------------------------
# KeepAlive triggering — Deepgram NET-0001 closes idle streams ~10 s; we send
# KeepAlive every ~5 s of silence. Pure function; time is mocked as arguments.
# ---------------------------------------------------------------------------


class TestKeepAlive:
    def test_interval_beats_deepgram_idle_timeout(self):
        assert KEEPALIVE_INTERVAL_SECONDS < 10.0  # NET-0001 margin

    def test_not_due_while_audio_flows(self):
        now = 100.0
        assert not keepalive_due(now, last_audio_at=99.5, last_keepalive_at=0.0)

    def test_due_after_5s_of_silence(self):
        now = 100.0
        assert keepalive_due(now, last_audio_at=95.0, last_keepalive_at=0.0)
        assert not keepalive_due(now, last_audio_at=95.1, last_keepalive_at=0.0)

    def test_not_resent_within_interval_of_last_keepalive(self):
        now = 100.0
        # Silent long enough, but a KeepAlive just went out → wait.
        assert not keepalive_due(now, last_audio_at=90.0, last_keepalive_at=97.0)
        # ...and due again once the last KeepAlive is an interval old.
        assert keepalive_due(now, last_audio_at=90.0, last_keepalive_at=95.0)

    def test_audio_resets_the_clock(self):
        assert not keepalive_due(100.0, last_audio_at=98.0, last_keepalive_at=50.0)


# ---------------------------------------------------------------------------
# Final-segment persistence mapping + contract envelopes (mocked relay events —
# no Deepgram involved, spec §2.3).
# ---------------------------------------------------------------------------


class TestFinalPersistenceMapping:
    def test_final_event_maps_to_transcript_segment_row(self):
        assembler = SegmentAssembler("enc_abc123")
        event = RelayTranscript(
            text="I stopped taking lisinopril two weeks ago.",
            is_final=True,
            ts=12.3456,
            speaker_hint=0,
        )
        segment = assembler.next_segment(event)
        row = segment_row_values("enc_abc123", segment)
        assert row == {
            "id": "seg_abc123_1",
            "encounter_id": "enc_abc123",
            "speaker": "doctor",  # first speaker heard → doctor
            "text": "I stopped taking lisinopril two weeks ago.",
            "ts": 12.346,  # seconds from session start (float, rounded)
            "is_final": True,
        }

    def test_interim_events_are_never_persist_shaped(self):
        # Only FINAL segments persist; interims exist solely as envelopes.
        assembler = SegmentAssembler("enc_abc123")
        interim = assembler.next_segment(RelayTranscript(text="I stop", is_final=False, ts=12.0))
        envelope = TranscriptInterimMessage(segment=interim)
        parsed = ServerMessageAdapter.validate_json(envelope.model_dump_json())
        assert parsed.type == "transcript.interim"
        assert assembler.final_count == 0  # interim did not consume a final id

    def test_final_envelope_round_trips_per_contract(self):
        assembler = SegmentAssembler("enc_abc123")
        segment = assembler.next_segment(
            RelayTranscript(text="Blood pressure was one fifty over ninety five.",
                            is_final=True, ts=30.0, speaker_hint=1)
        )
        envelope = TranscriptFinalMessage(segment=segment)
        parsed = ServerMessageAdapter.validate_json(envelope.model_dump_json())
        assert parsed.type == "transcript.final"
        assert parsed.segment.id == "seg_abc123_1"
        assert parsed.segment.ts == pytest.approx(30.0)
