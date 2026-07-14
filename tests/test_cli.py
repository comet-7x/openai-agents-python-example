"""CLI behavior tests: a single turn's failure must not tear down the session.

Offline: a fake orchestrator raises mid-stream instead of calling an LLM."""

from __future__ import annotations

import httpx
from comet import cli
from comet.agents.stream import StreamChunk
from comet.display.display import format_clock, format_duration
from comet.display.view import SystemView


def test_format_duration_rolls_over() -> None:
    # Settled per-turn summary: seconds → minutes → hours, no fractional seconds.
    assert format_duration(0) == "0s"
    assert format_duration(45.7) == "45s"
    assert format_duration(83) == "1m 23s"
    assert format_duration(600) == "10m 00s"
    assert format_duration(3723) == "1h 02m"


def test_format_clock_is_fixed_width() -> None:
    # Live ticking clock: zero-padded mm:ss so the panel title doesn't jitter,
    # rolling into an hours field past 60 minutes.
    assert format_clock(0) == "00:00"
    assert format_clock(9) == "00:09"
    assert format_clock(83) == "01:23"
    assert format_clock(3723) == "1:02:03"


def test_model_concurrency_is_wired_from_config() -> None:
    # LLM_MAX_CONCURRENCY must actually gate the shared model's semaphore — an
    # unwired default (100) lets N subagents + the orchestrator flood a limited
    # backend, which stalls the last request with no visible progress.
    from comet import config

    assert cli._model._semaphore._value == config.LLM_MAX_CONCURRENCY


def test_session_flag_default_convention() -> None:
    # Omitted → None (anonymous/in-memory); bare --session → "default"; with a
    # value → that value. The bare form is the convention this test locks in.
    p = cli._build_parser()
    assert p.parse_args([]).session is None
    assert p.parse_args(["--session"]).session == "default"
    assert p.parse_args(["--session", "proj"]).session == "proj"


class _RaisingOrchestrator:
    """Stands in for OrchestratorAgent: streams a chunk, then fails like a stalled
    backend (the real crash was an httpx.ReadTimeout from run_streamed)."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def run_streamed(self, _prompt: str):
        yield StreamChunk(content="thinking...", type="text")
        raise self._error


def test_finish_tool_settles_on_completed() -> None:
    # finish() ends the run, so its tool_output is the turn's completion — the
    # indicator must read "Completed", not revert to "Waiting…".
    ui = cli.RobustStreamUI()
    ui.on_chunk(
        StreamChunk(
            content='{"summary": "done"}',
            type="tool_call",
            tool_name="finish",
            tool_args={"summary": "done"},
            tool_call_id="c1",
        )
    )
    ui.on_chunk(StreamChunk(content="done", type="tool_output", tool_call_id="c1"))
    assert ui._status_msg == "Completed"


async def test_run_stream_survives_midstream_timeout() -> None:
    # A ReadTimeout mid-stream must be swallowed so the REPL loop keeps going.
    orch = _RaisingOrchestrator(httpx.ReadTimeout("backend went silent"))
    await cli._run_stream(orch, "extract this syllabus", SystemView())  # type: ignore[arg-type]


async def test_run_stream_survives_generic_error() -> None:
    # Non-network failures must not crash the session either.
    orch = _RaisingOrchestrator(ValueError("boom"))
    await cli._run_stream(orch, "do a thing", SystemView())  # type: ignore[arg-type]
