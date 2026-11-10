"""Orchestrator wiring: event publishing, the shared session workspace, and skill
discovery. No LLM turn is run — only construction and the synchronous paths."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from agents import RawResponsesStreamEvent, RunItemStreamEvent
from agents.items import ReasoningItem, ToolCallItem
from agents.run import Runner
from comet import config
from comet.agents.orchestrator import OrchestratorAgent, OrchestratorHooks
from comet.agents.protocol import (
    Criterion,
    CriterionVerdict,
    SubagentRequest,
    SubagentResponse,
    merge_acceptance,
)
from comet.agents.worker import WorkerAgent
from comet.display.view import SystemView
from comet.message_bus import events as ev
from comet.message_bus.bus import MessageBus
from comet.message_bus.sinks.jsonl_logger import JsonlLogger
from comet.skills.registry import SkillsRegistry
from comet.tools.filesystem import make_write_tool
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


async def test_anonymous_session_is_in_memory(model, monkeypatch, tmp_path) -> None:
    # No session_id (no --session) → ephemeral: no on-disk db, nothing to resume.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    orch = OrchestratorAgent(model=model)
    assert orch.persistent is False
    assert orch.resuming is False
    assert not (orch.session_workspace / "session.db").exists()
    orch._session.close()


async def test_named_session_persists_and_resumes(model, monkeypatch, tmp_path) -> None:
    # A named session file-backs its conversation; re-opening the same name resumes it.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)

    first = OrchestratorAgent(model=model, session_id="proj")
    assert first.persistent is True
    assert first.resuming is False  # brand new — no prior db
    db = first.session_workspace / "session.db"
    assert db.exists()  # SQLiteSession created the file
    # Persist a message so there is history to resume, then close the connection.
    await first._session.add_items([{"role": "user", "content": "remember me"}])
    first._session.close()

    second = OrchestratorAgent(model=model, session_id="proj")
    assert second.resuming is True  # same name, db already on disk
    items = await second._session.get_items()
    assert any(i.get("content") == "remember me" for i in items)  # history survived
    second._session.close()


async def test_blank_session_id_is_anonymous(model, monkeypatch, tmp_path) -> None:
    # An empty/whitespace name must normalize to anonymous (not a "persistent" session
    # pointing at a timestamped dir it could never resume): persistence and naming key
    # off the same predicate.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    for blank in ("", "   "):
        orch = OrchestratorAgent(model=model, session_id=blank)
        assert orch.persistent is False
        assert orch.session_id.startswith("sess_")  # fell back to timestamp
        assert not (orch.session_workspace / "session.db").exists()
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
        # Mirrors RunResultStreaming: usage is read off context_wrapper once the
        # stream drains (orchestrator publishes UsageReported from it).
        context_wrapper = SimpleNamespace(usage=None)

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
        context_wrapper = SimpleNamespace(usage=None)

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


async def test_streamed_reports_usage(model, monkeypatch, tmp_path) -> None:
    # Once the stream drains, the orchestrator publishes its own UsageReported
    # (read off context_wrapper.usage) so it's counted in the session total —
    # otherwise an orchestrator-only turn shows all-zero tokens in the view.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    bus = MessageBus()
    view = SystemView()
    bus.subscribe(view.apply)
    orch = OrchestratorAgent(model=model, bus=bus)

    usage = SimpleNamespace(requests=1, input_tokens=10, output_tokens=5, total_tokens=15)

    class _FakeStreaming:
        context_wrapper = SimpleNamespace(usage=usage)

        def stream_events(self):
            async def gen():
                for e in ():  # no stream events — we only exercise the drain/report
                    yield e

            return gen()

    with patch.object(Runner, "run_streamed", return_value=_FakeStreaming()):
        async for _ in orch.run_streamed("hi"):
            pass

    assert view.usage.total_tokens == 15
    assert view.usage.input_tokens == 10
    assert view.usage.requests == 1
    orch._session.close()


def test_skills_discovered() -> None:
    names = {s.name for s in SkillsRegistry(config.SKILLS_DIR).list_skills()}
    assert "xlsx" in names


async def test_prompt_inlines_skill_metadata(model, monkeypatch, tmp_path) -> None:
    # Level-1 skill metadata (name + description) is baked into the system prompt
    # each turn, so the orchestrator knows what exists without a list_skills call.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    orch = OrchestratorAgent(model=model)

    prompt = orch._build_instructions(None, None)
    for skill in orch._skills.list_skills():
        assert f"- {skill.name}: {skill.description}" in prompt
    assert "xlsx" in prompt  # the discovered library is present, not a placeholder
    orch._session.close()


# ── Delegation contract + verification gate ──────────────────────────────────


def test_request_render_and_merge_acceptance() -> None:
    # The rendered request surfaces the acceptance criteria (with check_mode) to the
    # worker, and merge overlays extra criteria onto the base, deduped by id.
    req = SubagentRequest(
        task="do the thing",
        inputs=["a.md"],
        acceptance=[Criterion(id="c1", describe="file exists", check_mode="mechanical")],
    )
    text = req.render()
    assert "do the thing" in text and "a.md" in text
    assert "(mechanical) file exists" in text  # criterion + its check mode are shown

    base = [Criterion(id="x", describe="base-x"), Criterion(id="y", describe="base-y")]
    extra = [Criterion(id="x", describe="override-x"), Criterion(id="z", describe="new-z")]
    merged = {c.id: c.describe for c in merge_acceptance(base, extra)}
    assert merged == {"x": "override-x", "y": "base-y", "z": "new-z"}  # later wins, no dup


def test_skill_declares_acceptance() -> None:
    # A skill's frontmatter acceptance is parsed and exposed as raw dicts; a skill
    # without one yields []. course-knowledge-extraction ships a baseline.
    reg = SkillsRegistry(config.SKILLS_DIR)
    crit = reg.load_acceptance("course-knowledge-extraction")
    ids = {c["id"] for c in crit}
    assert {"deliverables_exist", "faithful_to_source"} <= ids
    assert all(c.get("id") and c.get("describe") for c in crit)
    assert reg.load_acceptance("xlsx") == []  # declares none


def test_malformed_acceptance_skill_is_skipped(tmp_path) -> None:
    # An acceptance entry missing `describe` is malformed → the skill is skipped at
    # discovery (not silently half-loaded), while a well-formed sibling loads fine.
    good = tmp_path / "good"
    good.mkdir()
    (good / "SKILL.md").write_text(
        "---\nname: good\ndescription: d\nacceptance:\n  - id: a\n    describe: x\n---\nbody\n"
    )
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text(
        "---\nname: bad\ndescription: d\nacceptance:\n  - id: a\n---\nbody\n"
    )
    reg = SkillsRegistry(tmp_path)
    names = {s.name for s in reg.list_skills()}
    assert "good" in names and "bad" not in names
    assert reg.load_acceptance("good") == [{"id": "a", "describe": "x"}]


async def test_orchestrator_has_workspace_execution_tools(model, monkeypatch, tmp_path) -> None:
    # The orchestrator does the spine inline (not a pure coordinator): read_file +
    # list_dir for mechanical verification, plus write_file/edit_file/shell for
    # producing & assembling deliverables, plus the verify gate tool.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    orch = OrchestratorAgent(model=model)
    names = {t.name for t in orch._agent.tools}
    assert {"read_file", "list_dir", "write_file", "edit_file", "shell", "verify"} <= names
    orch._session.close()


async def test_orchestrator_file_tools_are_workspace_confined(model, monkeypatch, tmp_path) -> None:
    # The orchestrator's write/edit tools are the same hard-confined filesystem tools
    # the workers use: a path escaping the session workspace is rejected, and a write
    # inside it lands in the shared workspace dir.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    orch = OrchestratorAgent(model=model)
    write_file = make_write_tool(orch._fs_ctx)

    assert "escapes the workspace" in await write_file("../escape.txt", "x")

    ok = await write_file("deliverable.md", "hello")
    assert "Wrote" in ok
    assert (orch.session_workspace / "deliverable.md").read_text() == "hello"
    orch._session.close()


async def test_finish_is_gated_on_verification(model, monkeypatch, tmp_path) -> None:
    # finish() is refused until a clean verify(); a failing verdict keeps it closed,
    # an all-pass opens it, and the tool_use_behavior gate mirrors the same flag.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    orch = OrchestratorAgent(model=model)

    assert orch._verified is False
    assert "refused" in orch.finish("done")  # can't finish before verifying

    # A failing verdict does not open the gate.
    msg = orch.verify([CriterionVerdict(criterion_id="c1", verdict="fail", evidence="empty file")])
    assert orch._verified is False and "failing" in msg
    assert "refused" in orch.finish("done")

    # All-pass opens it; finish now returns the summary and publishes status.
    bus_status: list[str] = []
    orch._bus.subscribe(
        lambda e: bus_status.append(e.status) if isinstance(e, ev.OrchestratorStatus) else None
    )
    ok = orch.verify([CriterionVerdict(criterion_id="c1", verdict="pass", evidence="12KB, valid")])
    assert orch._verified is True and "verified" in ok
    assert orch.finish("all done") == "all done"
    assert bus_status == ["all done"]

    # The gate: a finish tool-result terminates the run only while verified.
    finish_result = [SimpleNamespace(tool=SimpleNamespace(name="finish"), output="all done")]
    res = await orch._finish_gate(None, finish_result)
    assert res.is_final_output is True and res.final_output == "all done"
    orch._verified = False
    res = await orch._finish_gate(None, finish_result)
    assert res.is_final_output is False  # unverified finish must not stop the run
    orch._session.close()


async def test_invoke_merges_skill_acceptance_into_prompt(model, monkeypatch, tmp_path) -> None:
    # invoke_subagent folds the equipped skill's declared criteria into the worker
    # prompt automatically (deterministic "两者叠加"), alongside task-specific ones.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    orch = OrchestratorAgent(model=model)
    msg = await orch.spawn_subagent("worker", skills=["course-knowledge-extraction"])
    sid = msg.split("id=")[1]

    captured: dict[str, str] = {}

    async def fake_run(prompt: str) -> None:
        captured["prompt"] = prompt

    entry = orch._pool.get(sid)
    assert entry is not None
    entry.run = fake_run  # capture the rendered request, don't run it
    await orch.invoke_subagent(
        sid,
        task="extract knowledge",
        acceptance=[Criterion(id="extra", describe="task-specific check", check_mode="mechanical")],
    )
    prompt = captured["prompt"]
    assert "extract knowledge" in prompt
    assert "task-specific check" in prompt  # orchestrator's own criterion
    assert "The expected deliverables exist" in prompt  # skill's declared criterion, auto-merged

    await orch.delete_all()
    orch._session.close()


# ── Subagent token streaming (opt-in, display-only) ──────────────────────────


def test_subagent_output_reduces_to_tail() -> None:
    # SubagentOutput deltas accumulate into a rolling per-subagent tail; a new turn or
    # a non-running state clears it so the row settles.
    view = SystemView()
    view.apply(ev.SubagentSpawned(id="s1", name="w"))
    view.apply(ev.SubagentState(id="s1", state="running"))
    view.apply(ev.SubagentOutput(id="s1", delta="Hel", channel="text"))
    view.apply(ev.SubagentOutput(id="s1", delta="lo", channel="text"))
    assert view.subagents["s1"].output_tail == "Hello"

    view.apply(ev.SubagentTurn(id="s1"))  # next turn → fresh tail
    assert view.subagents["s1"].output_tail == ""

    view.apply(ev.SubagentOutput(id="s1", delta="thinking...", channel="thinking"))
    view.apply(ev.SubagentState(id="s1", state="done"))  # settled → cleared
    assert view.subagents["s1"].output_tail == ""


async def test_stream_subagents_flag_flows_to_worker(model, monkeypatch, tmp_path) -> None:
    # The opt-in flag reaches the spawned WorkerAgent (which then uses run_streamed);
    # default is off.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    on = OrchestratorAgent(model=model, stream_subagents=True)
    e_on = await on._pool.create("w", "role")
    assert e_on.stream_output is True

    off = OrchestratorAgent(model=model)
    e_off = await off._pool.create("w", "role")
    assert e_off.stream_output is False

    await on.delete_all()
    await off.delete_all()
    on._session.close()
    off._session.close()


def _fake_streaming(events: list, final: str = "") -> object:
    class _Streaming:
        final_output = final
        context_wrapper = SimpleNamespace(usage=None)

        def stream_events(self):
            async def gen():
                for e in events:
                    yield e

            return gen()

    return _Streaming()


async def test_execute_streaming_publishes_deltas(model, tmp_path) -> None:
    # _execute in streaming mode drives run_streamed and publishes a SubagentOutput
    # per reasoning/visible delta (both channels), while still returning the final result.
    bus = MessageBus()
    outs: list[tuple[str, str]] = []
    bus.subscribe(
        lambda e: outs.append((e.channel, e.delta)) if isinstance(e, ev.SubagentOutput) else None
    )
    worker = WorkerAgent(
        id="s1",
        name="w",
        model=model,
        objective="role",
        workspace_dir=tmp_path,
        bus=bus,
        stream_output=True,
    )

    def rtext(s: str) -> RawResponsesStreamEvent:
        data = ResponseTextDeltaEvent.model_construct(delta=s, type="response.output_text.delta")
        return RawResponsesStreamEvent(data=data)

    def rreason(s: str) -> RawResponsesStreamEvent:
        data = ResponseReasoningTextDeltaEvent.model_construct(
            delta=s, type="response.reasoning_text.delta"
        )
        return RawResponsesStreamEvent(data=data)

    streaming = _fake_streaming([rreason("think"), rtext("ans")], final="ans")
    with patch.object(Runner, "run_streamed", return_value=streaming):
        result = await worker._execute("go")

    assert ("thinking", "think") in outs and ("text", "ans") in outs
    assert result.final_output == "ans"  # parent still gets the final result
    worker.conv_session.close()


async def test_execute_nonstreaming_emits_no_output(model, tmp_path) -> None:
    # Default (stream_output=False) uses Runner.run and publishes NO SubagentOutput.
    bus = MessageBus()
    outs: list[object] = []
    bus.subscribe(lambda e: outs.append(e) if isinstance(e, ev.SubagentOutput) else None)
    worker = WorkerAgent(
        id="s2",
        name="w",
        model=model,
        objective="role",
        workspace_dir=tmp_path,
        bus=bus,
    )

    async def fake_run(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(final_output="done")

    with patch.object(Runner, "run", new=fake_run):
        result = await worker._execute("go")

    assert result.final_output == "done" and outs == []
    worker.conv_session.close()


def test_jsonl_logger_excludes_subagent_output(tmp_path) -> None:
    # Token-level SubagentOutput is dropped from the audit trace; lifecycle events stay.
    logger = JsonlLogger(tmp_path / "run.jsonl")
    logger.handle(ev.SubagentOutput(id="s1", delta="tok", channel="text"))
    logger.handle(ev.SubagentStatus(id="s1", status="working"))
    logger.close()
    lines = (tmp_path / "run.jsonl").read_text().splitlines()
    assert len(lines) == 1 and "SubagentStatus" in lines[0]
    assert "SubagentOutput" not in (tmp_path / "run.jsonl").read_text()


# ── Structured worker report (fallback: fenced json in the final message) ─────


def test_response_parse_and_render() -> None:
    # The worker appends a fenced json block; parse recovers it, render flattens it.
    text = (
        "Converted the syllabus.\n\n"
        "```json\n"
        '{"status": "done", '
        '"manifest": [{"path": "out.md", "kind": "file", "detail": "13KB"}], '
        '"summary": "converted to markdown", "blockers": ""}\n'
        "```"
    )
    r = SubagentResponse.parse(text)
    assert r.status == "done" and r.summary == "converted to markdown"
    assert r.manifest[0].path == "out.md" and r.manifest[0].detail == "13KB"
    rendered = r.render()
    assert "status: done" in rendered and "out.md (file) — 13KB" in rendered

    # No block, or an invalid one → the whole message degrades to summary, never a crash.
    assert SubagentResponse.parse("plain prose").summary == "plain prose"
    assert "not valid" in SubagentResponse.parse("```json\n{not valid}\n```").summary


async def test_wait_for_result_parses_worker_report(model, monkeypatch, tmp_path) -> None:
    # wait_for_subagent_result parses the worker's fenced report into a clean
    # status/manifest/summary and consumes the task.
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    orch = OrchestratorAgent(model=model)
    sid = (await orch.spawn_subagent("w")).split("id=")[1]
    entry = orch._pool.get(sid)
    assert entry is not None

    final = (
        "done.\n```json\n"
        '{"status": "done", "manifest": [{"path": "r.md", "kind": "file", "detail": "1KB"}], '
        '"summary": "ok", "blockers": ""}\n```'
    )

    async def _result() -> SimpleNamespace:
        return SimpleNamespace(final_output=final)

    entry.run_task = asyncio.ensure_future(_result())
    out = await orch.wait_for_subagent_result(sid)
    assert "status: done" in out and "r.md (file) — 1KB" in out and "summary: ok" in out
    assert entry.run_task is None  # consumed

    await orch.delete_all()
    orch._session.close()
