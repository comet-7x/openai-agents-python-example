"""Message bus, SystemView reducer, and JSONL sink."""

from __future__ import annotations

import json

from comet.display.view import SystemView
from comet.message_bus import events as ev
from comet.message_bus.bus import MessageBus
from comet.message_bus.sinks.jsonl_logger import JsonlLogger


def test_pubsub_and_unsubscribe() -> None:
    bus = MessageBus()
    got: list[ev.Event] = []
    unsubscribe = bus.subscribe(got.append)
    bus.publish(ev.OrchestratorTurn())
    assert len(got) == 1
    unsubscribe()
    bus.publish(ev.OrchestratorTurn())
    assert len(got) == 1  # no longer delivered


def test_bad_sink_is_isolated() -> None:
    def bad_sink(_event: ev.Event) -> None:
        raise RuntimeError("boom")

    bus = MessageBus()
    bus.subscribe(bad_sink)
    got: list[ev.Event] = []
    bus.subscribe(got.append)
    bus.publish(ev.OrchestratorTurn())  # must not raise
    assert len(got) == 1  # the good sink still ran


def test_systemview_reduces_events() -> None:
    view = SystemView()
    view.apply(ev.OrchestratorTurn())
    view.apply(ev.OrchestratorStatus(status="planning"))
    view.apply(ev.OrchestratorSkills(skills=("xlsx",)))
    view.apply(ev.SubagentSpawned(id="s1", name="A"))
    view.apply(ev.SubagentState(id="s1", state="running"))
    view.apply(ev.SubagentTurn(id="s1"))
    view.apply(ev.SubagentStatus(id="s1", status="working"))
    view.apply(ev.SubagentCommand(id="s1", command="ls -la"))

    assert view.orchestrator.turns == 1
    assert view.orchestrator.status == "planning"
    assert view.orchestrator.loaded_skills == ["xlsx"]
    sub = view.subagents["s1"]
    assert (sub.state, sub.turns, sub.status, sub.last_command) == (
        "running",
        1,
        "working",
        "ls -la",
    )

    view.apply(ev.SubagentDeleted(id="s1"))
    assert view.subagent_list() == []


def test_systemview_sums_usage() -> None:
    view = SystemView()
    view.apply(
        ev.UsageReported(
            source="orchestrator",
            requests=2,
            input_tokens=100,
            output_tokens=40,
            total_tokens=140,
            elapsed_s=1.5,
        )
    )
    view.apply(
        ev.UsageReported(
            source="s1",
            requests=1,
            input_tokens=30,
            output_tokens=10,
            total_tokens=40,
            elapsed_s=0.5,
        )
    )
    u = view.usage
    assert (u.requests, u.input_tokens, u.output_tokens, u.total_tokens) == (3, 130, 50, 180)
    assert u.runs == 2
    assert u.agent_seconds == 2.0


def test_usage_from_usage_is_duck_typed() -> None:
    class FakeUsage:
        requests, input_tokens, output_tokens, total_tokens = 1, 10, 5, 15

    e = ev.UsageReported.from_usage("s1", FakeUsage(), 0.25)
    assert (e.source, e.total_tokens, e.elapsed_s) == ("s1", 15, 0.25)
    # None (an errored run with no result) degrades to zero tokens, keeps time.
    z = ev.UsageReported.from_usage("s2", None, 0.9)
    assert (z.total_tokens, z.elapsed_s) == (0, 0.9)


def test_jsonl_logger_captures_spawn_skills(tmp_path) -> None:
    logger = JsonlLogger(tmp_path / "trace.jsonl")
    logger.handle(ev.SubagentSpawned(id="s1", name="A", skills=("xlsx",)))
    logger.handle(ev.SubagentDeleted(id="s1"))
    logger.close()

    lines = [json.loads(x) for x in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert lines[0]["event"] == "SubagentSpawned"
    assert lines[0]["skills"] == ["xlsx"]
    assert lines[1]["event"] == "SubagentDeleted"
