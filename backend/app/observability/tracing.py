"""Langfuse v4 tracing helpers — encounter-rooted traces (spec §20).

The encounter trace id is minted at encounter start and PERSISTED on the
encounter row (Postgres). Every later span attaches to that stored id
explicitly via the SDK v4 ``trace_context={"trace_id": ...}`` option on
``start_as_current_observation`` — verified for real against the local
Langfuse v4 server (spans land under the given trace id and are retrievable
via GET /api/public/v2/observations?traceId=...). Default per-request SDK
tracing would NOT produce the single per-encounter tree, hence the explicit
attachment.

Nesting: only the OUTERMOST span of a processing cycle passes trace_context;
spans opened while another span of the same tracer is active inherit the
OTel context and nest naturally (per-condition subagent spans under
extract_patient_facts, context_assembly under the model-call span, etc.).

Degradation contract (spec §2.4 / §31): if Langfuse is unconfigured or down,
every helper becomes a silent no-op — the AI loop NEVER crashes or blocks on
observability. The degraded state is logged once.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_client: Any = None
_client_lock = threading.Lock()
_client_failed = False


def get_langfuse_client() -> Any:
    """Module-level Langfuse client, or None when unconfigured/unavailable."""
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed:
        return None
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    with _client_lock:
        if _client is not None or _client_failed:
            return _client
        try:
            from langfuse import Langfuse

            _client = Langfuse(
                host=settings.langfuse_host,
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
            )
        except Exception as exc:  # noqa: BLE001 — degraded, never crash
            logger.warning("Langfuse client init failed — tracing degraded to no-op: %s", exc)
            _client_failed = True
            return None
    return _client


def reset_client_for_tests() -> None:
    global _client, _client_failed
    _client = None
    _client_failed = False


class SpanHandle:
    """Failure-proof wrapper around a Langfuse span (or nothing)."""

    __slots__ = ("_span",)

    def __init__(self, span: Any = None) -> None:
        self._span = span

    def update(self, **kwargs: Any) -> None:
        if self._span is None:
            return
        try:
            self._span.update(**kwargs)
        except Exception:  # noqa: BLE001
            logger.debug("Langfuse span.update failed (ignored)", exc_info=True)


@dataclass
class EncounterTracer:
    """Span helpers rooted at a persisted encounter trace id.

    ``trace_id`` must be 32 lowercase hex chars (``uuid4().hex`` — exactly
    what app.encounters mints and persists). Invalid/absent ids degrade to
    per-cycle standalone traces rather than crashing.
    """

    trace_id: str | None = None

    def __post_init__(self) -> None:
        self._depth: contextvars.ContextVar[int] = contextvars.ContextVar(
            f"careloop_span_depth_{id(self)}", default=0
        )
        if self.trace_id and not _TRACE_ID_RE.match(self.trace_id):
            logger.warning(
                "Encounter trace_id %r is not 32-hex — spans will not attach to it.",
                self.trace_id,
            )
            self.trace_id = None

    @property
    def enabled(self) -> bool:
        return get_langfuse_client() is not None

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        *,
        as_type: str = "span",
        input: Any = None,  # noqa: A002 — mirrors langfuse kwarg
        metadata: dict | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
    ):
        """Open a span attached to the encounter trace. Yields a SpanHandle.

        Errors raised by the BODY propagate (real pipeline errors are not
        swallowed); errors from Langfuse itself never do.
        """
        client = get_langfuse_client()
        if client is None:
            yield SpanHandle()
            return

        meta = dict(metadata or {})
        if prompt_version:
            meta["prompt_version"] = prompt_version

        depth = self._depth.get()
        kwargs: dict[str, Any] = {
            "name": name,
            "as_type": as_type,
            "input": input,
            "metadata": meta or None,
        }
        if model is not None:
            kwargs["model"] = model
        if depth == 0 and self.trace_id:
            kwargs["trace_context"] = {"trace_id": self.trace_id}

        cm = None
        span_obj = None
        try:
            cm = client.start_as_current_observation(**kwargs)
            span_obj = cm.__enter__()
        except Exception:  # noqa: BLE001
            logger.debug("Langfuse span open failed for %s (no-op)", name, exc_info=True)
            cm = None
            span_obj = None

        token = self._depth.set(depth + 1)
        try:
            yield SpanHandle(span_obj)
        finally:
            self._depth.reset(token)
            if cm is not None:
                try:
                    cm.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    logger.debug("Langfuse span close failed for %s", name, exc_info=True)

    # ------------------------------------------------------------------
    # Named helpers for the spec §20 spans
    # ------------------------------------------------------------------

    def extract_patient_facts(self, *, transcript_chars: int, metadata: dict | None = None):
        meta = {"transcript_chars": transcript_chars, **(metadata or {})}
        return self.span("extract_patient_facts", metadata=meta)

    def condition_subagent(self, condition: str, *, prompt_version: str | None = None):
        """Per-condition subagent child span (spec §26) under extract_patient_facts."""
        return self.span(f"extract_{condition}", metadata={"condition": condition},
                         prompt_version=prompt_version)

    def generate_next_best_action(self, *, prompt_version: str | None = None):
        return self.span("generate_next_best_action", prompt_version=prompt_version)

    def generation(self, task: str, *, model: str | None, prompt_version: str | None,
                   input: Any = None):  # noqa: A002
        """Model-call span (as_type=generation) — update with usage_details after."""
        return self.span(task, as_type="generation", model=model,
                         prompt_version=prompt_version, input=input)

    @contextlib.contextmanager
    def context_assembly(self, *, task: str, fact_ids: list[str], token_estimate: int):
        """spec §20 required span: which fact ids + token estimate went into a call."""
        with self.span(
            "context_assembly",
            metadata={"task": task, "fact_ids": fact_ids, "fact_count": len(fact_ids),
                      "token_estimate": token_estimate},
        ) as handle:
            yield handle

    def flush(self) -> None:
        client = get_langfuse_client()
        if client is None:
            return
        try:
            client.flush()
        except Exception:  # noqa: BLE001
            logger.debug("Langfuse flush failed (ignored)", exc_info=True)


def record_usage(handle: SpanHandle, usage: dict | None) -> None:
    """Attach OpenAI usage to a generation span (input/output/total tokens)."""
    if not usage:
        return
    details = {}
    if usage.get("input_tokens") is not None:
        details["input"] = usage["input_tokens"]
    if usage.get("output_tokens") is not None:
        details["output"] = usage["output_tokens"]
    if usage.get("total_tokens") is not None:
        details["total"] = usage["total_tokens"]
    if details:
        handle.update(usage_details=details)
