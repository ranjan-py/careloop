"""Warm Langfuse trace — `python -m app.observability.warm_trace` (spec §30).

The final step of `make reset-demo`: ONE real OpenAI call wrapped in a
Langfuse span, flushed, trace id printed — so the observability tab is never
empty after a reset. Mirrors app.smoke.langfuse_smoke without the retrieval
polling (Langfuse v3 ingestion is async; the trace appears shortly after).

Anti-fake rule (spec §2.1): missing keys → BLOCKED on stderr and exit 3.
A trace is never fabricated. Exit 1 on real-call failure.
"""

from __future__ import annotations

import asyncio
import sys

from pydantic import BaseModel

from app.ai.client import call_model
from app.config import get_settings


class WarmTraceExtraction(BaseModel):
    medication: str
    status: str


async def warm_trace() -> int:
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", settings.openai_api_key),
            ("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key),
            ("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key),
        )
        if not value
    ]
    if missing:
        print(
            f"BLOCKED: {', '.join(missing)} not set — warm trace requires a REAL "
            "OpenAI call inside a REAL Langfuse span (never faked).",
            file=sys.stderr,
        )
        return 3

    try:
        from langfuse import Langfuse

        lf = Langfuse(
            host=settings.langfuse_host,
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
        )
        with lf.start_as_current_observation(name="warm_trace", as_type="span") as span:  # SDK v4 API
            trace_id = lf.get_current_trace_id()
            parsed = await call_model(
                task="warm_trace",
                instructions=(
                    "Extract the medication name and its reported status from the "
                    "sentence. Synthetic demo data — not medical advice."
                ),
                input_text="The patient reports he stopped taking lisinopril two weeks ago.",
                output_schema=WarmTraceExtraction,
                prompt_version="warm-trace-v1",
            )
            span.update(output={"medication": parsed.medication, "status": parsed.status})
        lf.flush()
    except Exception as exc:  # noqa: BLE001 — report the real failure, never fake
        print(f"warm trace FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not trace_id:
        print("warm trace FAILED: Langfuse SDK returned no trace id.", file=sys.stderr)
        return 1
    print(f"Warm trace generated: trace_id={trace_id}")
    print("Langfuse v3 ingestion is async — allow a few seconds before it appears in the UI.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(warm_trace()))
