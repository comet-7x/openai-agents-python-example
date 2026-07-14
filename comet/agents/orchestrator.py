import asyncio
import json
import time
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from agents import (
    Agent,
    AgentHooks,
    ItemHelpers,
    ModelSettings,
    OpenAIChatCompletionsModel,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
    ToolsToFinalOutputResult,
    function_tool,
)
from agents.items import (
    MessageOutputItem,
    ReasoningItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from agents.memory import SQLiteSession
from agents.run import Runner
from openai.types.responses import (
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseTextDeltaEvent,
)

from comet import config
from comet.agents.protocol import (
    Criterion,
    CriterionVerdict,
    SubagentRequest,
    SubagentResponse,
    merge_acceptance,
)
from comet.agents.stream import StreamChunk
from comet.agents.worker_pool import WorkerAgentPool
from comet.message_bus import events as ev
from comet.message_bus.bus import MessageBus
from comet.skills.registry import SkillError, SkillsRegistry
from comet.tools.context import ToolContext
from comet.tools.filesystem import make_list_tool, make_read_tool

ORCHESTRATOR_INSTRUCTIONS = """
You are a relentless execution agent. Your only job is to get the user's task fully done - not \
partially done, not blocked on questions, fully done. Do not stop. Do not ask for clarification \
unless you have exhausted every reasonable interpretation and attempt.

Today is: {time}

## Default assumptions
- The user wants working code. Write it, run it, fix it until it works.
- Subagents have access to a local shell. Installing dependencies, cloning repos, \
downloading data - this is all expected work, not a blocker. Just do it.
- If something is unclear, make a reasonable assumption, proceed, and note the assumption in your \
finish() summary.
- Creativity and persistence are the job. If one approach fails, try another. Adapt.

## Subagent instructions
Write your subagent objectives and task messages with the same mindset: assume the work is expected, \
assume setup is needed, and instruct them to persist. Never let a subagent stop just because something \
wasn't pre-installed or pre-configured. Subagents should be told to figure it out.

## Status updates
Before every tool call, call set_status with a one-sentence description of what you are about to do, \
unless status is unchanged. One sentence, no filler.

## Skills
These skills are available (name - one-line description):
{skills}
When a task matches one, call load_skill(name) to pull in its full operational
guidance, then follow it closely. (list_skills re-lists the same set at any time.)
Some skills bundle reference docs or scripts - use list_skill_resources and
read_skill_reference to pull those in only when you actually need them; hand
script paths to a worker agent's shell tool to run them.

## Delegating work
When you invoke_subagent, give it acceptance criteria: concrete, checkable statements
of what "done" requires. Tag each criterion's check_mode:
- "mechanical" - verifiable by inspecting the workspace (a file exists / is non-empty /
  parses / has the right shape). You will check these yourself.
- "judgment" - requires reading and reasoning about content quality (faithfulness, no
  hallucination, correct organization). These need an independent verifier.
Criteria a skill declares are folded in automatically; add task-specific ones on top.

## Verification loop (run before you finish - this is not optional)
A subagent's report is a CLAIM, not proof. Your "done" must be justified by evidence
YOU gathered, not by the worker's self-report. Before finishing:
1. For each MECHANICAL criterion: check ground truth yourself with list_dir and
   read_file (you can read the shared workspace directly). Confirm files exist, are
   non-empty, and look right - don't trust the worker's manifest.
2. For each JUDGMENT criterion: spawn a NEW, independent verifier subagent. Give it
   only the acceptance criteria and the artifact paths - NOT the producer's summary or
   reasoning (that would just anchor it). Have it judge the artifacts against the
   criteria and report pass/fail with specific defects.
3. Keep verification proportional: sample and spot-check, never redo the work.
4. Call verify(...) with a verdict for every criterion. If any fails, fix it (re-delegate)
   and re-verify - do not finish. Only once every criterion passes may you call finish;
   finish is gated on a clean verify and will be refused otherwise.
"""


def _format_skills(skills: list[Any]) -> str:
    """Level-1 (METADATA) skills overview: one `- name: description` line per
    skill. Shared by the system prompt (so the orchestrator always *knows* which
    skills exist, no discovery tool-call required) and the list_skills tool (which
    just re-lists on demand)."""
    if not skills:
        return "(none available)"
    return "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)


class OrchestratorHooks(AgentHooks):  # type: ignore[type-arg]
    """Lifecycle hooks to track orchestrator state for the live display."""

    def bind(self, orchestrator: "OrchestratorAgent") -> None:
        self._orchestrator = orchestrator

    async def on_llm_start(
        self, context: Any, agent: Any, system_prompt: Any, input_items: Any
    ) -> None:
        # A new LLM call = a new turn. Start it in `waiting` (request in flight, no
        # output yet). The streamed loop promotes this to `thinking` or `responding`
        # once deltas arrive; the non-streamed path has no deltas, so it stays
        # `waiting` until a tool runs or the run ends — honest, rather than claiming
        # the model is 'thinking' when it may just be time-to-first-token.
        self._orchestrator._bus.publish(ev.OrchestratorTurn())
        self._orchestrator._bus.publish(ev.OrchestratorPhase(phase="waiting"))

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        self._orchestrator._bus.publish(ev.OrchestratorTool(tool=tool.name))
        self._orchestrator._bus.publish(ev.OrchestratorPhase(phase="calling_tool"))

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        self._orchestrator._bus.publish(ev.OrchestratorTool(tool=None))

    async def on_end(self, context: Any, agent: Any, output: Any) -> None:
        # Run finished (final output produced): no LLM call in flight.
        self._orchestrator._bus.publish(ev.OrchestratorPhase(phase="idle"))
        self._orchestrator._bus.publish(ev.OrchestratorTool(tool=None))


class OrchestratorAgent:
    """Orchestrator that manages a pool of local subagents to complete user tasks."""

    def __init__(
        self,
        model: OpenAIChatCompletionsModel,
        bus: MessageBus | None = None,
        session_id: str | None = None,
        stream_subagents: bool = False,
    ) -> None:
        self._bus = bus or MessageBus()

        # One workspace per session, shared by all subagents so their files
        # (reports, data) are visible to each other and persist across turns.
        # Normalize the name once so persistence and workspace-naming key off the SAME
        # predicate: an empty/whitespace `--session ""` is anonymous, not a "named"
        # session pointing at a timestamped dir it could never resume.
        name = (session_id or "").strip()
        self.persistent = bool(name)
        self.session_id = name or datetime.now(config.TIMEZONE).strftime("sess_%Y%m%d_%H%M%S")
        self.session_workspace = config.WORKSPACES_ROOT / self.session_id
        self.session_workspace.mkdir(parents=True, exist_ok=True)

        # A *named* session (via `--session NAME`) persists its conversation to a SQLite
        # file in the workspace, so `run()` resumes it after a restart; an anonymous
        # (timestamped) session stays in-memory and is discarded on exit. `resuming` is
        # true only when that file already exists from a prior run.
        _db_file = self.session_workspace / "session.db"
        self.resuming = self.persistent and _db_file.exists()

        self._pool = WorkerAgentPool(
            model=model,
            bus=self._bus,
            session_workspace=self.session_workspace,
            stream_subagents=stream_subagents,
        )
        self._skills = SkillsRegistry(config.SKILLS_DIR)

        # Read-only view of the shared session workspace, so the orchestrator can
        # verify mechanical acceptance criteria against ground truth itself (list_dir /
        # read_file) instead of spawning a subagent just to `stat` a file. Confined to
        # the workspace by filesystem._resolve, exactly like the worker file tools.
        self._fs_ctx = ToolContext(workspace_dir=self.session_workspace)

        # Finish gate: set True only by a clean verify() call, reset at the start of
        # every run. The callable tool_use_behavior (_finish_gate) refuses to let
        # finish() terminate the run unless this is True — a hard gate, not a prompt hope.
        self._verified = False

        # Kept as None for display.py compatibility
        self._reasoning_effort: None = None
        self.status: str | None = None
        self.loaded_skills: list[str] = []
        # Turn count, current tool, and phase are tracked in the SystemView from the
        # events published below — the orchestrator holds no display-state copies.

        orchestrator_hooks = OrchestratorHooks()
        orchestrator_hooks.bind(self)

        self._agent = Agent(
            name="Orchestrator Agent",
            model=model,
            instructions=self._build_instructions,
            # include_usage=True so token counts arrive even with a custom
            # LLM_BASE_URL: the SDK only sets stream_options={"include_usage": True}
            # by default for the official OpenAI client, else streams zero usage.
            model_settings=ModelSettings(tool_choice="auto", include_usage=True),
            # finish() terminates the run only through this gate: it refuses unless a
            # clean verify() has set self._verified (see _finish_gate). Replaces the
            # old StopAtTools(["finish"]), which stopped unconditionally on the call.
            tool_use_behavior=self._finish_gate,
            hooks=orchestrator_hooks,
            tools=[
                function_tool(self.set_status),
                function_tool(self.list_skills),
                function_tool(self.load_skill),
                function_tool(self.list_skill_resources),
                function_tool(self.read_skill_reference),
                function_tool(make_read_tool(self._fs_ctx)),
                function_tool(make_list_tool(self._fs_ctx)),
                function_tool(self.spawn_subagent),
                function_tool(self.list_subagents),
                function_tool(self.invoke_subagent),
                function_tool(self.wait_for_subagent_result),
                function_tool(self.wait_for_first_subagent_result),
                function_tool(self.delete_subagent),
                function_tool(self.delete_all),
                function_tool(self.verify),
                function_tool(self.finish),
            ],
        )
        self._session = SQLiteSession(
            session_id="orchestrator",
            db_path=str(_db_file) if self.persistent else ":memory:",
        )

    def _build_instructions(self, _ctx: Any, _agent: Any) -> str:
        """Build the system prompt fresh each turn (the SDK invokes this per run).

        Rebuilding matters twice over: the embedded timestamp stays current across
        a long interactive session, and the Level-1 skills overview reflects the
        live registry — so the orchestrator always knows which skills exist without
        first spending a list_skills tool-call to discover them."""
        now = datetime.now(config.TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %A")
        return ORCHESTRATOR_INSTRUCTIONS.format(
            time=now, skills=_format_skills(self._skills.list_skills())
        )

    async def run(self, prompt: str) -> str:
        """Run a single turn with the orchestrator.

        Emits events on `self._bus`; attaching a live display is the caller's job
        (see cli.py). The orchestrator no longer knows about any display.
        """
        self._verified = False  # each task must earn its own clean verify() before finish
        start = time.perf_counter()
        result = await Runner.run(
            self._agent,
            prompt,
            max_turns=200,
            session=self._session,
        )
        self._bus.publish(
            ev.UsageReported.from_usage(
                "orchestrator", result.context_wrapper.usage, time.perf_counter() - start
            )
        )
        return result.final_output or ""

    async def run_streamed(self, prompt: str) -> AsyncGenerator[StreamChunk, None]:
        """Stream orchestrator events in real time.

        Yields StreamChunk objects as the orchestrator thinks, calls tools,
        and receives tool results. The final summary arrives as a tool_call
        chunk with tool_name="finish".
        """
        self._verified = False  # each task must earn its own clean verify() before finish
        start = time.perf_counter()
        streaming = Runner.run_streamed(
            self._agent,
            prompt,
            max_turns=200,
            session=self._session,
        )
        # The hook only sets `waiting` on LLM start; the reasoning→content transition
        # is observable only here, so this loop promotes `waiting` to `thinking` (first
        # reasoning delta) and `responding` (first visible-content delta). Deduped so we
        # publish one event per transition, not per delta (each is logged by JSONL too).
        streamed_phase: str = ""

        def _phase(p: ev.Phase) -> None:
            nonlocal streamed_phase
            if p != streamed_phase:
                streamed_phase = p
                self._bus.publish(ev.OrchestratorPhase(phase=p))

        async for event in streaming.stream_events():
            if isinstance(event, RawResponsesStreamEvent):
                if isinstance(event.data, ResponseTextDeltaEvent):
                    _phase("responding")
                    yield StreamChunk(content=event.data.delta, type="text")
                elif isinstance(
                    event.data,
                    (ResponseReasoningTextDeltaEvent, ResponseReasoningSummaryTextDeltaEvent),
                ):
                    # Two reasoning channels exist: `*TextDeltaEvent` streams the
                    # detailed `content` reasoning, `*SummaryTextDeltaEvent` streams the
                    # `summary` reasoning that o-series and many OpenAI-compatible
                    # backends actually populate (leaving `content` empty). Listening to
                    # only one leaves reasoning blank on the others, so we treat both as
                    # `thinking`.
                    #
                    # Phase is monotonic within a turn: once we've started emitting
                    # the visible answer, a late/interleaved reasoning delta (some
                    # backends stream reasoning and content interleaved) must not flip
                    # the indicator back to `thinking`. The reasoning text is still
                    # surfaced as a chunk; only the coarse phase latches. The tracker
                    # resets at the tool-call boundary, so the next turn thinks afresh.
                    if streamed_phase != "responding":
                        _phase("thinking")
                    yield StreamChunk(content=event.data.delta, type="thinking")

            elif isinstance(event, RunItemStreamEvent):
                item = event.item
                if isinstance(item, ToolCallItem):
                    raw = item.raw_item
                    call_id = getattr(raw, "call_id", None)
                    tool_name = getattr(raw, "name", None)
                    args_str = getattr(raw, "arguments", None) or "{}"
                    try:
                        args = json.loads(args_str)
                    except json.JSONDecodeError:
                        args = {"_raw": args_str}
                    yield StreamChunk(
                        content=args_str,
                        type="tool_call",
                        tool_name=tool_name,
                        tool_args=args,
                        tool_call_id=call_id,
                    )
                    # The hook moves us to `calling_tool`; clear the tracker so the
                    # next turn's first delta re-emits its phase (a turn that resumes
                    # straight into content would otherwise be missed as stale).
                    streamed_phase = ""
                elif isinstance(item, ToolCallOutputItem):
                    raw = item.raw_item
                    call_id = (
                        raw.get("call_id")
                        if isinstance(raw, dict)
                        else getattr(raw, "call_id", None)
                    )
                    yield StreamChunk(
                        content=str(item.output),
                        type="tool_output",
                        tool_call_id=call_id,
                    )
                elif isinstance(item, ReasoningItem):
                    raw = item.raw_item
                    # Prefer the detailed `content`; fall back to `summary`, where
                    # o-series and many OpenAI-compatible backends put reasoning while
                    # leaving `content` empty (the empty-reasoning bug). Mirrors the
                    # dual-channel handling of the streamed reasoning deltas above.
                    text = "".join(c.text for c in (raw.content or []) if hasattr(c, "text"))
                    if not text:
                        text = "".join(s.text for s in (raw.summary or []) if hasattr(s, "text"))
                    if text:
                        yield StreamChunk(content=text, type="thinking_done")
                elif isinstance(item, MessageOutputItem):
                    text = ItemHelpers.text_message_output(item)
                    if text:
                        yield StreamChunk(content=text, type="message_done")

        # Stream drained — publish the run's token usage (from context_wrapper,
        # populated once the stream completes) so the orchestrator is counted in
        # the session total, mirroring each subagent's _on_done report.
        self._bus.publish(
            ev.UsageReported.from_usage(
                "orchestrator", streaming.context_wrapper.usage, time.perf_counter() - start
            )
        )

    ## Tools
    def set_status(self, status: str) -> str:
        """Set a one-sentence status summary shown in the live display.
        Call this before every tool invocation unless the status is unchanged."""
        self.status = status
        self._bus.publish(ev.OrchestratorStatus(status=status))
        return "Status updated."

    def list_skills(self) -> str:
        """Re-list the skills available to the orchestrator, one-line description each.
        The same set is already inlined in the system prompt; call this to refresh it."""
        return _format_skills(self._skills.list_skills())

    def load_skill(self, skill_name: str) -> str:
        """Load a skill's full instructions into context. Call after list_skills
        has identified a relevant skill."""
        try:
            instructions = self._skills.load_instructions(skill_name)
        except SkillError as e:
            return f"Error: {e}"
        if skill_name not in self.loaded_skills:
            self.loaded_skills.append(skill_name)
            self._bus.publish(ev.OrchestratorSkills(skills=tuple(self.loaded_skills)))
        return instructions

    def list_skill_resources(self, skill_name: str) -> str:
        """List the scripts/references/assets files bundled with a skill, without
        reading their contents. Use this before read_skill_reference, or to find a
        script path to hand to a worker agent's shell tool."""
        try:
            resources = self._skills.list_resources(skill_name)
        except SkillError as e:
            return f"Error: {e}"
        lines = [
            f"{d}/: {', '.join(files) if files else '(none)'}" for d, files in resources.items()
        ]
        return "\n".join(lines)

    def read_skill_reference(self, skill_name: str, ref_path: str) -> str:
        """Read a reference document bundled with a skill (path relative to
        that skill's references/ directory, as shown by list_skill_resources)."""
        try:
            return self._skills.read_reference(skill_name, ref_path)
        except SkillError as e:
            return f"Error: {e}"

    def list_subagents(self) -> str:
        """Check active subagents and their status. Returns IDs, names, task status, and turn counts."""
        entries = self._pool.list_entries()
        if not entries:
            return "No active subagents."
        return "\n".join(e.text_summary() for e in entries)

    async def spawn_subagent(
        self,
        agent_name: str,
        objective: str = "You are a general purpose coding agent with access to a local shell.",
        skills: list[str] | None = None,
    ) -> str:
        """Spawn a new local subagent with the given name and objective.
        Returns the subagent's unique ID, which must be used for subsequent invoke_subagent calls.

        The objective becomes the subagent's instructions (system prompt) - its persistent role
        throughout its lifetime. Write it as an identity statement describing who the agent is
        and what it owns, not as a task. Tasks are sent separately via invoke_subagent.

        Arguments:
            agent_name: A short human-readable label for this subagent.
            objective: The subagent's persistent role and scope (becomes its instructions/system prompt).
            skills: Optional skill names (see list_skills) to equip this subagent with. Each
                skill's full instructions are folded into the subagent's objective at spawn time,
                fixed for its lifetime - skills cannot be added to a subagent after it is spawned.
        """
        extra_tools = []
        if skills:
            skill_sections = []
            for skill_name in skills:
                skill = self._skills.get(skill_name)
                if skill is None:
                    return f"Error: skill '{skill_name}' not found"
                try:
                    body = self._skills.load_instructions(skill_name)
                except SkillError as e:
                    return f"Error: {e}"
                extra_tools.extend(self._skills.load_tools(skill_name))
                skill_sections.append(f"### {skill_name} (files at {skill.path.resolve()})\n{body}")
            objective = objective + "\n\n## Skills\n" + "\n\n".join(skill_sections)

        entry = await self._pool.create(
            agent_name, objective, extra_tools=extra_tools, skills=tuple(skills or ())
        )
        self._bus.publish(
            ev.SubagentSpawned(id=entry.id, name=entry.name, skills=tuple(skills or ()))
        )
        return f"Subagent '{agent_name}' spawned with id={entry.id}"

    async def delete_subagent(self, agent_id: str) -> str:
        """Terminate a subagent and free its resources when done."""
        entry = self._pool.get(agent_id)
        if entry is None:
            return f"Subagent '{agent_id}' not found."
        await self._pool.delete(agent_id)
        self._bus.publish(ev.SubagentDeleted(id=agent_id))
        return f"Subagent '{agent_id}' terminated."

    async def delete_all(self) -> None:
        """Terminate all subagent sessions."""
        await self._pool.delete_all()

    async def invoke_subagent(
        self,
        agent_id: str,
        task: str,
        inputs: list[str] | None = None,
        acceptance: list[Criterion] | None = None,
    ) -> str:
        """Start a task on a subagent and return immediately (non-blocking).

        Arguments:
            agent_id: the subagent to task (from spawn_subagent).
            task: what to do — write it as relentless, self-sufficient instructions.
            inputs: paths/artifacts the subagent should read.
            acceptance: acceptance criteria (the definition-of-done) you will verify the
                result against; tag each with check_mode "mechanical" or "judgment".
                Criteria declared by the subagent's equipped skills are merged in
                automatically, so only add task-specific ones here.

        Use wait_for_subagent_result or wait_for_first_subagent_result to get the result.
        """
        entry = self._pool.get(agent_id)
        if entry is None:
            return f"Subagent '{agent_id}' not found. Use spawn_subagent first."
        if entry.run_task is not None and not entry.run_task.done():
            return f"Subagent '{agent_id}' is still running. Use wait_for_subagent_result to await its result."
        # Fold in each equipped skill's declared baseline acceptance criteria (raw
        # dicts → Criterion), overlaid by the orchestrator's task-specific ones (later
        # wins by id). Done here deterministically — the "两者叠加" rule — rather than
        # trusting the model to copy a skill's criteria into every call.
        skill_criteria: list[Criterion] = []
        for skill_name in entry.skills:
            try:
                skill_criteria.extend(
                    Criterion(**c) for c in self._skills.load_acceptance(skill_name)
                )
            except SkillError:
                continue
        request = SubagentRequest(
            task=task,
            inputs=inputs or [],
            acceptance=merge_acceptance(skill_criteria, acceptance or []),
        )
        await entry.run(request.render())
        return f"Subagent '{agent_id}' task started."

    async def wait_for_subagent_result(self, agent_id: str) -> str:
        """Block until a specific subagent finishes and return its result."""
        entry = self._pool.get(agent_id)
        if entry is None:
            return f"Subagent '{agent_id}' not found."
        if entry.run_task is None:
            return f"Subagent '{agent_id}' has no active task."
        result = await entry.run_task
        entry.run_task = None
        # The worker appends a fenced json report to its final message (the backend
        # rejects structured output_type); recover it into a clean status + manifest +
        # summary. A worker that ignored the format degrades to summary-only, never an error.
        return SubagentResponse.parse(result.final_output or "").render()

    async def wait_for_first_subagent_result(self) -> str:
        """Block until any running subagent finishes and return its ID and result."""
        running = {
            e.run_task: e
            for e in self._pool._entries.values()
            if e.run_task and not e.run_task.done()
        }
        if not running:
            return "No running subagents."
        done, _ = await asyncio.wait(running.keys(), return_when=asyncio.FIRST_COMPLETED)
        entry = running[next(iter(done))]
        result = await self.wait_for_subagent_result(entry.id)
        return f"Subagent '{entry.id}' ({entry.name}) completed: {result}"

    def verify(self, verification: list[CriterionVerdict]) -> str:
        """Record your verification of the acceptance criteria — the gate finish()
        must pass. Provide one verdict (pass/fail + the evidence you observed) per
        acceptance criterion you set on the task. Call this only after you have
        actually checked ground truth (mechanical criteria via list_dir/read_file,
        judgment criteria via an independent verifier subagent).

        Arguments:
            verification: one CriterionVerdict per acceptance criterion.
        """
        if not verification:
            self._verified = False
            return "No verdicts given. Provide a pass/fail verdict for every acceptance criterion."
        failed = [v for v in verification if v.verdict == "fail"]
        if failed:
            self._verified = False
            lines = "\n".join(f"- {v.criterion_id}: {v.evidence}" for v in failed)
            return (
                f"{len(failed)} criterion(s) still failing — fix them (re-delegate) and "
                f"re-verify before finishing:\n{lines}"
            )
        self._verified = True
        return f"All {len(verification)} criteria verified. You may call finish now."

    def finish(self, summary: str) -> str:
        """Signal completion with a concise summary of what was accomplished,
        including key outputs, file paths, or results produced.

        Gated: this only ends the run after a clean verify(). If called before the
        acceptance criteria have been verified, it is refused and you must run the
        verification loop first.

        Arguments:
            summary: A concise summary of what was accomplished.
        """
        if not self._verified:
            return (
                "finish refused: no clean verification on record. Run the verification "
                "loop and call verify() with every acceptance criterion passing first."
            )
        self.status = summary
        self._bus.publish(ev.OrchestratorStatus(status=summary))
        return summary

    async def _finish_gate(self, _ctx: Any, results: list[Any]) -> ToolsToFinalOutputResult:
        """Custom tool_use_behavior (replaces StopAtTools): end the run only when
        finish() is called *and* a clean verify() has set self._verified. On an
        unverified finish, keep running — finish() itself returned the refusal message,
        which the model receives and acts on, so it must verify before it can stop."""
        for r in results:
            if getattr(r.tool, "name", None) == "finish" and self._verified:
                return ToolsToFinalOutputResult(is_final_output=True, final_output=r.output)
        return ToolsToFinalOutputResult(is_final_output=False)
