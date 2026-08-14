#!/usr/bin/env python
"""REAL end-to-end replay check against the RUNNING stack (spec §8/§34).

Run from the host:  backend/.venv/bin/python scripts/verify_replay.py

Flow (no mocks anywhere — real login, real WS, real Deepgram behind it):
  1. httpx login as the demo clinician
  2. POST /api/encounters {patient_id: "john-miller"} → encounter.stream_ticket
  3. open ws://localhost:8002/api/encounters/{id}/stream
  4. session.start mode=replay → server streams the bundled fixture through
     the real Deepgram live path at real-time pace
  5. collect events ~75 s, then session.end → expect session.finalizing +
     session.finalized (server flushes Deepgram trailing finals first)
  6. GET the encounter → persisted transcript must cover the finals seen live

Asserts: ≥1 conn.status "connected"; ≥5 transcript.final envelopes; scripted
terms present ("blood pressure" or "lisinopril"); finalizing+finalized seen;
persisted segments ≥ finals seen. Prints PASS/FAIL summary; exit 0/1.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import websockets

BASE_URL = "http://localhost:8002"
WS_BASE = "ws://localhost:8002"
EMAIL = "maya.patel@careloop.demo"
PASSWORD = "demo-only-password"
PATIENT_ID = "john-miller"

COLLECT_SECONDS = 75.0
FINALIZE_TIMEOUT_SECONDS = 45.0  # CloseStream + trailing-final grace window
MIN_FINALS = 5
SCRIPTED_TERMS = ("blood pressure", "lisinopril")


def _fail(msg: str) -> None:
    print(f"FAIL  {msg}")


def _ok(msg: str) -> None:
    print(f"ok    {msg}")


async def main() -> int:
    failures: list[str] = []

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        # 1. Real login (session cookie)
        resp = await client.post(
            "/api/auth/login", json={"email": EMAIL, "password": PASSWORD}
        )
        resp.raise_for_status()
        clinician = resp.json()["clinician"]["name"]
        _ok(f"logged in as {clinician}")

        # 2. Create encounter, take the one-time stream ticket
        resp = await client.post("/api/encounters", json={"patient_id": PATIENT_ID})
        resp.raise_for_status()
        encounter = resp.json()["encounter"]
        encounter_id = encounter["id"]
        ticket = encounter.get("stream_ticket")
        if not ticket:
            _fail("POST /api/encounters returned no stream_ticket")
            return 1
        _ok(f"created encounter {encounter_id} (ticket issued)")

        # 3–5. WebSocket replay session
        connected_statuses = 0
        status_seq: list[str] = []
        interims = 0
        finals: list[dict] = []
        trailing_finals = 0
        errors: list[dict] = []
        saw_finalizing = False
        saw_finalized = False

        ws_url = f"{WS_BASE}/api/encounters/{encounter_id}/stream"
        async with websockets.connect(ws_url, max_size=2**23) as ws:
            await ws.send(
                json.dumps({"type": "session.start", "mode": "replay", "ticket": ticket})
            )
            _ok(f"session.start mode=replay sent; collecting ~{COLLECT_SECONDS:.0f}s of events")

            deadline = time.monotonic() + COLLECT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                except websockets.ConnectionClosed:
                    failures.append("WS closed by server during collection window")
                    break
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "conn.status":
                    status_seq.append(msg.get("deepgram", "?"))
                    if msg.get("deepgram") == "connected":
                        connected_statuses += 1
                        print(f"      conn.status connected — {msg.get('detail', '')}")
                    else:
                        print(f"      conn.status {msg.get('deepgram')} — {msg.get('detail', '')}")
                elif mtype == "transcript.interim":
                    interims += 1
                elif mtype == "transcript.final":
                    finals.append(msg["segment"])
                    seg = msg["segment"]
                    print(
                        f"      final #{len(finals)} [{seg['speaker']:>7} @ {seg['ts']:7.2f}s] "
                        f"{seg['text'][:80]}"
                    )
                elif mtype == "error":
                    errors.append(msg)
                    print(f"      error [{msg.get('scope')}] {msg.get('message')}")

            # session.end → finalizing / finalized (+ any trailing finals)
            await ws.send(json.dumps({"type": "session.end"}))
            print("      session.end sent; awaiting finalizing/finalized")
            end_deadline = time.monotonic() + FINALIZE_TIMEOUT_SECONDS
            while not saw_finalized:
                remaining = end_deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    break
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "session.finalizing":
                    saw_finalizing = True
                    _ok("session.finalizing received")
                elif mtype == "session.finalized":
                    saw_finalized = True
                    _ok(f"session.finalized received (encounter_id={msg.get('encounter_id')})")
                elif mtype == "transcript.final":
                    trailing_finals += 1  # stragglers flushed by CloseStream

        # 6. Persistence check via REST
        resp = await client.get(f"/api/encounters/{encounter_id}")
        resp.raise_for_status()
        body = resp.json()
        persisted = body.get("transcript", [])
        encounter_status = body.get("encounter", {}).get("status")

    # ---------------- assertions ----------------
    if connected_statuses >= 1:
        _ok(f"conn.status connected seen {connected_statuses}x (sequence: {status_seq})")
    else:
        failures.append(f"no conn.status connected (sequence: {status_seq})")

    if len(finals) >= MIN_FINALS:
        _ok(f"{len(finals)} transcript.final envelopes in collection window (≥{MIN_FINALS})")
    else:
        failures.append(f"only {len(finals)} transcript.final envelopes (need ≥{MIN_FINALS})")

    transcript_text = " ".join(seg["text"] for seg in finals).lower()
    terms_found = [t for t in SCRIPTED_TERMS if t in transcript_text]
    if terms_found:
        _ok(f"scripted terms present: {terms_found}")
    else:
        failures.append(
            f"none of the scripted terms {SCRIPTED_TERMS} found in "
            f"{len(transcript_text)} chars of finals"
        )

    if saw_finalizing:
        _ok("session.finalizing observed after session.end")
    else:
        failures.append("session.finalizing not received after session.end")
    if saw_finalized:
        _ok("session.finalized observed")
    else:
        failures.append("session.finalized not received")

    if len(persisted) >= len(finals):
        _ok(
            f"persisted transcript segments: {len(persisted)} ≥ finals seen live: "
            f"{len(finals)} (+{trailing_finals} trailing during finalize)"
        )
    else:
        failures.append(
            f"persisted transcript segments {len(persisted)} < finals seen live {len(finals)}"
        )

    print()
    print("=" * 72)
    verdict = "PASS" if not failures else "FAIL"
    print(
        f"verify_replay: {verdict}  "
        f"(encounter={encounter_id} status={encounter_status} "
        f"connected={connected_statuses} interims={interims} finals={len(finals)} "
        f"trailing_finals={trailing_finals} persisted={len(persisted)} "
        f"ws_errors={len(errors)})"
    )
    for f in failures:
        _fail(f)
    print("=" * 72)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
