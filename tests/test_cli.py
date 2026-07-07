"""CLI behavior tests: a single turn's failure must not tear down the session.

Offline: a fake orchestrator raises mid-stream instead of calling an LLM."""

from __future__ import annotations

import httpx
from comet import cli
from comet.agents.stream import StreamChunk
from comet.display.view import SystemView


class _RaisingOrchestrator:
    """Stands in for OrchestratorAgent: streams a chunk, then fails like a stalled
    backend (the real crash was an httpx.ReadTimeout from run_streamed)."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def run_streamed(self, _prompt: str):
        yield StreamChunk(content="thinking...", type="text")
        raise self._error


async def test_run_stream_survives_midstream_timeout() -> None:
    # A ReadTimeout mid-stream must be swallowed so the REPL loop keeps going.
    orch = _RaisingOrchestrator(httpx.ReadTimeout("backend went silent"))
    await cli._run_stream(orch, "extract this syllabus", SystemView())  # type: ignore[arg-type]


async def test_run_stream_survives_generic_error() -> None:
    # Non-network failures must not crash the session either.
    orch = _RaisingOrchestrator(ValueError("boom"))
    await cli._run_stream(orch, "do a thing", SystemView())  # type: ignore[arg-type]
