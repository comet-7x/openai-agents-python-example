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


def test_jsonl_logger_captures_spawn_skills(tmp_path) -> None:
    logger = JsonlLogger(tmp_path / "trace.jsonl")
    logger.handle(ev.SubagentSpawned(id="s1", name="A", skills=("xlsx",)))
    logger.handle(ev.SubagentDeleted(id="s1"))
    logger.close()

    lines = [json.loads(x) for x in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert lines[0]["event"] == "SubagentSpawned"
    assert lines[0]["skills"] == ["xlsx"]
    assert lines[1]["event"] == "SubagentDeleted"
