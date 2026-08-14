"""Dedicated event-loop thread for the AI pipeline (spec §9).

Model calls, DB writes, and graph projection for the live encounter run on
their OWN event loop so the realtime audio path (Deepgram relay + browser WS
at ~10 frames/s + interim fan-out) can never contend with them. Observed
before isolation: intermittent multi-minute stalls of in-loop OpenAI calls
during replay, including sessions where zero suggestions surfaced live.

Loop-affine resources (SQLAlchemy engine, Neo4j driver, AsyncOpenAI client)
are cached per event loop by their modules, so code running here transparently
gets pipeline-loop-local instances.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any

logger = logging.getLogger(__name__)

_executor: PipelineExecutor | None = None
_lock = threading.Lock()


class PipelineExecutor:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="ai-pipeline-loop", daemon=True
        )
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError("AI pipeline loop failed to start")
        logger.info("AI pipeline event loop started (thread=%s)", self._thread.name)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._started.set)
        self._loop.run_forever()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future:
        """Schedule a coroutine on the pipeline loop; returns a concurrent Future."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Await a coroutine on the pipeline loop from any other loop."""
        return await asyncio.wrap_future(self.submit(coro))


def get_executor() -> PipelineExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = PipelineExecutor()
        return _executor
