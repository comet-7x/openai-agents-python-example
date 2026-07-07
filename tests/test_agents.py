"""Orchestrator wiring: event publishing, the shared session workspace, and skill
discovery. No LLM turn is run — only construction and the synchronous paths."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from agents import RawResponsesStreamEvent, RunItemStreamEvent
from agents.items import ReasoningItem, ToolCallItem
from agents.run import Runner
from comet import config
from comet.agents.orchestrator import OrchestratorAgent, OrchestratorHooks
from comet.display.view import SystemView
from comet.message_bus import events as ev
from comet.message_bus.bus import MessageBus
from comet.skills.registry import SkillsRegistry
from openai.types.responses import (
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseTextDeltaEvent,
)


async def test_orchestrator_publishes_to_view(model, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    bus = MessageBus()
    view = SystemView()
    bus.subscribe(view.apply)
    orch = OrchestratorAgent(model=model, bus=bus)

    orch.set_status("planning")
    orch.load_skill("xlsx")
    msg = await orch.spawn_subagent("analyst", skills=["xlsx"])
    sid = msg.split("id=")[1]

    assert view.orchestrator.status == "planning"
    assert view.orchestrator.loaded_skills == ["xlsx"]
    assert view.subagents[sid].name == "analyst"

    await orch.delete_subagent(sid)
    assert sid not in view.subagents
    orch._session.close()


async def test_shared_session_workspace(model, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    orch = OrchestratorAgent(model=model)

    a = await orch._pool.create("A", "role A")
    b = await orch._pool.create("B", "role B")
    assert a.workspace_dir == b.workspace_dir == orch.session_workspace

    (a.workspace_dir / "report.md").write_text("# Draft\n", encoding="utf-8")
    assert (b.workspace_dir / "report.md").exists()  # sibling sees the deliverable

    await orch.delete_all()
    orch._session.close()


async def test_orchestrator_phase_transitions(model, monkeypatch, tmp_path) -> None:
    # Coarse phase from the lifecycle hooks (the only signal the non-streamed path
    # gets): idle -> waiting (new turn, awaiting first token) -> calling_tool -> idle.
    # `thinking`/`responding` need deltas, so the hook path never reaches them.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    bus = MessageBus()
    view = SystemView()
    bus.subscribe(view.apply)
    orch = OrchestratorAgent(model=model, bus=bus)
    hooks = OrchestratorHooks()
    hooks.bind(orch)

    assert view.orchestrator.phase == "idle"
    await hooks.on_llm_start(None, None, None, None)
    assert view.orchestrator.phase == "waiting"
    assert view.orchestrator.turns == 1
    await hooks.on_tool_start(None, None, SimpleNamespace(name="shell"))
    assert view.orchestrator.phase == "calling_tool"
    assert view.orchestrator.current_tool == "shell"
    await hooks.on_end(None, None, "done")
    assert view.orchestrator.phase == "idle"
    assert view.orchestrator.current_tool is None
    # The orchestrator keeps no display-state copies of these.
    assert not any(hasattr(orch, a) for a in ("turns", "current_tool", "is_thinking"))
    orch._session.close()


async def test_streamed_phase_refinement(model, monkeypatch, tmp_path) -> None:
    # The streamed loop refines the coarse phase: `responding` is only observable
    # from the first visible-content delta, deduped to one event per transition.
    # The tool boundary resets the tracker so a turn that resumes into content
    # re-emits `responding` instead of being swallowed as stale.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    bus = MessageBus()
    phases: list[str] = []
    bus.subscribe(lambda e: phases.append(e.phase) if isinstance(e, ev.OrchestratorPhase) else None)
    orch = OrchestratorAgent(model=model, bus=bus)

    def rtext(s: str) -> RawResponsesStreamEvent:
        data = ResponseTextDeltaEvent.model_construct(delta=s, type="response.output_text.delta")
        return RawResponsesStreamEvent(data=data)

    def rreason(s: str) -> RawResponsesStreamEvent:
        data = ResponseReasoningTextDeltaEvent.model_construct(
            delta=s, type="response.reasoning_text.delta"
        )
        return RawResponsesStreamEvent(data=data)

    def rtool() -> RunItemStreamEvent:
        item = ToolCallItem(
            agent=orch._agent,
            raw_item=SimpleNamespace(call_id="c1", name="shell", arguments="{}"),  # type: ignore[arg-type]
        )
        return RunItemStreamEvent(name="tool_called", item=item)

    # reason, text, then an interleaved reason (must NOT flip back to thinking —
    # phase is monotonic within a turn), tool boundary (resets), text (re-emitted).
    events = [rreason("t"), rtext("a"), rreason("t2"), rtext("a2"), rtool(), rtext("b")]

    class _FakeStreaming:
        def stream_events(self):
            async def gen():
                for e in events:
                    yield e

            return gen()

    with patch.object(Runner, "run_streamed", return_value=_FakeStreaming()):
        async for _ in orch.run_streamed("hi"):
            pass

    assert phases == ["thinking", "responding", "responding"], phases
    orch._session.close()


async def test_streamed_reasoning_summary_channel(model, monkeypatch, tmp_path) -> None:
    # Reasoning delivered on the *summary* channel (o-series and many
    # OpenAI-compatible backends leave `content` empty and populate `summary`)
    # must still surface: summary deltas → `thinking`, and a ReasoningItem with
    # only `summary` set → `thinking_done`. This is the empty-reasoning fix.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    orch = OrchestratorAgent(model=model)

    def rsummary(s: str) -> RawResponsesStreamEvent:
        data = ResponseReasoningSummaryTextDeltaEvent.model_construct(
            delta=s, type="response.reasoning_summary_text.delta"
        )
        return RawResponsesStreamEvent(data=data)

    def reasoning_item(summary_text: str) -> RunItemStreamEvent:
        # content empty, summary populated — the shape that came back blank before.
        raw = SimpleNamespace(content=None, summary=[SimpleNamespace(text=summary_text)])
        item = ReasoningItem(agent=orch._agent, raw_item=raw)  # type: ignore[arg-type]
        return RunItemStreamEvent(name="reasoning_item_created", item=item)

    events = [rsummary("plan"), rsummary(" more"), reasoning_item("plan more done")]

    class _FakeStreaming:
        def stream_events(self):
            async def gen():
                for e in events:
                    yield e

            return gen()

    chunks: list[tuple[str, str]] = []
    with patch.object(Runner, "run_streamed", return_value=_FakeStreaming()):
        async for c in orch.run_streamed("hi"):
            chunks.append((c.type, c.content))

    assert ("thinking", "plan") in chunks  # summary deltas stream as reasoning
    assert ("thinking_done", "plan more done") in chunks  # summary fallback surfaced
    orch._session.close()


def test_skills_discovered() -> None:
    names = {s.name for s in SkillsRegistry(config.SKILLS_DIR).list_skills()}
    assert "xlsx" in names
