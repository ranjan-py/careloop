"""Real third-party smoke tests (spec §33). Run: python -m app.smoke [--json]

Three checks, each against the REAL configured service — no mocks, no canned
responses. Missing key/fixture → BLOCKED (never PASS). Results feed
VERIFICATION.md.

  openai    minimal structured request through the provider seam
  deepgram  stream a real WAV fixture through Deepgram live WS (linear16/16k)
  langfuse  a real OpenAI call inside a Langfuse span, then confirm the trace
            is retrievable from the local Langfuse API (async ingestion — polls)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import wave
from pathlib import Path

import httpx
from pydantic import BaseModel

from app.ai.client import ProviderNotConfiguredError, call_model
from app.config import get_settings

FIXTURE_RELPATH = Path("audio/miller_encounter.wav")


class SmokeExtraction(BaseModel):
    medication: str
    status: str


def _result(name: str, status: str, detail: str, **extra) -> dict:
    return {"check": name, "status": status, "detail": detail, **extra}


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


async def openai_smoke() -> dict:
    settings = get_settings()
    started = time.monotonic()
    try:
        parsed = await call_model(
            task="smoke_extraction",
            instructions=(
                "Extract the medication name and its reported status from the sentence. "
                "Synthetic demo data — not medical advice."
            ),
            input_text="The patient reports he stopped taking lisinopril two weeks ago.",
            output_schema=SmokeExtraction,
            prompt_version="smoke-v1",
        )
    except ProviderNotConfiguredError as exc:
        return _result("openai", "BLOCKED", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _result("openai", "FAIL", f"{type(exc).__name__}: {exc}")
    latency_ms = (time.monotonic() - started) * 1000
    ok = "lisinopril" in parsed.medication.lower()
    return _result(
        "openai",
        "PASS" if ok else "FAIL",
        f"model={settings.openai_model} extracted medication={parsed.medication!r} "
        f"status={parsed.status!r} latency={latency_ms:.0f}ms",
        latency_ms=round(latency_ms),
    )


# ---------------------------------------------------------------------------
# Deepgram — stream the fixture through the live WS (encoding=linear16)
# ---------------------------------------------------------------------------


async def deepgram_smoke() -> dict:
    settings = get_settings()
    if not settings.deepgram_api_key:
        return _result("deepgram", "BLOCKED", "DEEPGRAM_API_KEY not set — real integration not verified.")
    fixture = Path(settings.data_dir) / FIXTURE_RELPATH
    if not fixture.exists():
        return _result(
            "deepgram",
            "BLOCKED",
            f"Fixture {fixture} missing — generate it with scripts/generate_fixture.py "
            "(TTS; requires OPENAI_API_KEY), then re-run.",
        )

    with wave.open(str(fixture), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000:
            return _result(
                "deepgram",
                "FAIL",
                f"Fixture must be 16 kHz mono 16-bit PCM; got "
                f"{wav.getframerate()} Hz / {wav.getnchannels()} ch / {wav.getsampwidth() * 8}-bit.",
            )
        pcm = wav.readframes(wav.getnframes())

    import websockets

    url = (
        "wss://api.deepgram.com/v1/listen"
        f"?encoding=linear16&sample_rate=16000&model={settings.deepgram_model}"
        "&interim_results=true&smart_format=true"
    )
    headers = {"Authorization": f"Token {settings.deepgram_api_key}"}
    finals: list[str] = []
    started = time.monotonic()
    first_transcript_ms: float | None = None

    async def _connect():
        try:
            return await websockets.connect(url, additional_headers=headers)
        except TypeError:  # websockets < 12 uses extra_headers
            return await websockets.connect(url, extra_headers=headers)

    try:
        ws = await _connect()
    except Exception as exc:  # noqa: BLE001
        return _result("deepgram", "FAIL", f"WS connect failed: {type(exc).__name__}: {exc}")

    async def _sender():
        chunk = 3200  # 100 ms of 16 kHz mono Int16
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i : i + chunk])
            await asyncio.sleep(0.05)  # 2× real-time — fine for a smoke test
        await ws.send(json.dumps({"type": "CloseStream"}))

    async def _receiver():
        nonlocal first_transcript_ms
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "Results":
                continue
            alt = (msg.get("channel") or {}).get("alternatives") or [{}]
            text = alt[0].get("transcript", "")
            if text and first_transcript_ms is None:
                first_transcript_ms = (time.monotonic() - started) * 1000
            if text and msg.get("is_final"):
                finals.append(text)

    try:
        await asyncio.wait_for(asyncio.gather(_sender(), _receiver()), timeout=180)
    except asyncio.TimeoutError:
        return _result("deepgram", "FAIL", "Timed out streaming fixture to Deepgram.")
    except Exception as exc:  # noqa: BLE001
        if not finals:
            return _result("deepgram", "FAIL", f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    transcript = " ".join(finals).lower()
    if not transcript:
        return _result("deepgram", "FAIL", "No final transcript received from Deepgram.")
    ok = "lisinopril" in transcript or "blood pressure" in transcript
    return _result(
        "deepgram",
        "PASS" if ok else "FAIL",
        f"model={settings.deepgram_model} finals={len(finals)} "
        f"first_transcript={first_transcript_ms:.0f}ms "
        f"contains_scripted_terms={ok} sample={transcript[:120]!r}",
        first_transcript_ms=round(first_transcript_ms or 0),
    )


# ---------------------------------------------------------------------------
# Langfuse — real OpenAI call inside a span, trace visible in local Langfuse
# ---------------------------------------------------------------------------


async def langfuse_smoke() -> dict:
    settings = get_settings()
    if not settings.openai_api_key:
        return _result("langfuse", "BLOCKED", "Needs OPENAI_API_KEY (trace must wrap a REAL model call).")
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return _result("langfuse", "BLOCKED", "Langfuse keys not set.")

    try:
        from langfuse import Langfuse

        lf = Langfuse(
            host=settings.langfuse_host,
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
        )
        with lf.start_as_current_span(name="smoke_trace") as span:
            trace_id = lf.get_current_trace_id()
            parsed = await call_model(
                task="smoke_langfuse",
                instructions="Answer with the medication mentioned. Synthetic demo.",
                input_text="Patient stopped lisinopril due to dizziness.",
                output_schema=SmokeExtraction,
                prompt_version="smoke-v1",
            )
            span.update(output={"medication": parsed.medication})
        lf.flush()
    except Exception as exc:  # noqa: BLE001
        return _result("langfuse", "FAIL", f"Span/model call failed: {type(exc).__name__}: {exc}")

    if not trace_id:
        return _result("langfuse", "FAIL", "No trace id from Langfuse SDK.")

    # v3 ingestion is async (web → queue → worker → ClickHouse): poll the API.
    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()
    deadline = time.monotonic() + 90
    async with httpx.AsyncClient(timeout=10) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(
                    f"{settings.langfuse_host}/api/public/traces/{trace_id}",
                    headers={"Authorization": f"Basic {auth}"},
                )
                if resp.status_code == 200:
                    return _result(
                        "langfuse",
                        "PASS",
                        f"Trace {trace_id} ingested and retrievable from local Langfuse.",
                        trace_id=trace_id,
                    )
            except httpx.HTTPError:
                pass
            await asyncio.sleep(3)
    return _result(
        "langfuse",
        "FAIL",
        f"Trace {trace_id} not retrievable within 90 s (async ingestion — check worker/ClickHouse).",
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--only", choices=["openai", "deepgram", "langfuse"], help="run a single check"
    )
    args = parser.parse_args()

    checks = {
        "openai": openai_smoke,
        "deepgram": deepgram_smoke,
        "langfuse": langfuse_smoke,
    }
    if args.only:
        checks = {args.only: checks[args.only]}

    results = [await fn() for fn in checks.values()]

    if args.json:
        print(json.dumps({"results": results}, indent=2))
    else:
        for r in results:
            print(f"{r['check']:<10} {r['status']:<8} {r['detail']}")

    if any(r["status"] == "FAIL" for r in results):
        return 1
    if any(r["status"] == "BLOCKED" for r in results):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
