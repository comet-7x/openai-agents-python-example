import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from agents import (
    Agent,
    AgentHooks,
    ItemHelpers,
    ModelSettings,
    OpenAIChatCompletionsModel,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
    StopAtTools,
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
    ResponseReasoningTextDeltaEvent,
    ResponseTextDeltaEvent,
)

from comet import config
from comet.agents.worker_pool import WorkerAgentPool
from comet.message_bus import events as ev
from comet.message_bus.bus import MessageBus
from comet.skills.registry import SkillError, SkillsRegistry


@dataclass
class StreamChunk:
    """A single streamed event from the orchestrator.

    type values:
      "text"          — final-answer token delta (streamed, use to print in real time)
      "thinking"      — reasoning token delta (streamed; only fires on reasoning models)
      "tool_call"     — orchestrator called a tool (complete, not delta)
      "tool_output"   — tool returned a result (complete)
      "thinking_done" — full reasoning block finished (content = complete reasoning text)
      "message_done"  — the orchestrator's current message is complete

    "thinking" / "thinking_done" are emitted when the model supports reasoning
    (e.g. o1, o3). They are safe to ignore or use as a flag / debug trace.
    """

    content: str
    type: Literal[
        "text", "thinking", "tool_call", "tool_output", "thinking_done", "message_done"
    ] = "text"
    tool_name: str | None = field(default=None)
    tool_args: dict[str, Any] | None = field(default=None)
    tool_call_id: str | None = field(default=None)


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
Start each session by calling list_skills and loading any that are relevant.
Skills contain detailed operational guidance - follow them closely.
Some skills bundle reference docs or scripts - use list_skill_resources and
read_skill_reference to pull those in only when you actually need them; hand
script paths to a worker agent's shell tool to run them.
"""


class OrchestratorHooks(AgentHooks):  # type: ignore[type-arg]
    """Lifecycle hooks to track orchestrator state for the live display."""

    def bind(self, orchestrator: "OrchestratorAgent") -> None:
        self._orchestrator = orchestrator

    async def on_llm_start(
        self, context: Any, agent: Any, system_prompt: Any, input_items: Any
    ) -> None:
        self._orchestrator.turns += 1
        self._orchestrator.is_thinking = True
        self._orchestrator._bus.publish(ev.OrchestratorTurn())
        self._orchestrator._bus.publish(ev.OrchestratorThinking(thinking=True))

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        self._orchestrator.is_thinking = False
        self._orchestrator._bus.publish(ev.OrchestratorThinking(thinking=False))

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        self._orchestrator.current_tool = tool.name
        self._orchestrator._bus.publish(ev.OrchestratorTool(tool=tool.name))

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        self._orchestrator.current_tool = None
        self._orchestrator._bus.publish(ev.OrchestratorTool(tool=None))


class OrchestratorAgent:
    """Orchestrator that manages a pool of local subagents to complete user tasks."""

    def __init__(
        self,
        model: OpenAIChatCompletionsModel,
        bus: MessageBus | None = None,
        session_id: str | None = None,
    ) -> None:
        self.bus = bus or MessageBus()
        self._bus = self.bus  # internal alias used by hooks/tools

        # One workspace per session, shared by all subagents so their files
        # (reports, data) are visible to each other and persist across turns.
        self.session_id = session_id or datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
            "sess_%Y%m%d_%H%M%S"
        )
        self.session_workspace = config.WORKSPACES_ROOT / self.session_id
        self.session_workspace.mkdir(parents=True, exist_ok=True)

        self._pool = WorkerAgentPool(
            model=model, bus=self.bus, session_workspace=self.session_workspace
        )
        self._skills = SkillsRegistry(config.SKILLS_DIR)

        # Kept as None for display.py compatibility
        self._reasoning_effort: None = None
        self.turns: int = 0
        self.current_tool: str | None = None
        self.status: str | None = None
        self.is_thinking: bool = False
        self.loaded_skills: list[str] = []

        orchestrator_hooks = OrchestratorHooks()
        orchestrator_hooks.bind(self)

        self._agent = Agent(
            name="Orchestrator Agent",
            model=model,
            instructions=ORCHESTRATOR_INSTRUCTIONS.format(
                time=lambda: datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
                    "%Y-%m-%d %H:%M:%S %A"
                )
            ),
            model_settings=ModelSettings(tool_choice="auto"),
            tool_use_behavior=StopAtTools(stop_at_tool_names=["finish"]),
            hooks=orchestrator_hooks,
            tools=[
                function_tool(self.set_status),
                function_tool(self.list_skills),
                function_tool(self.load_skill),
                function_tool(self.list_skill_resources),
                function_tool(self.read_skill_reference),
                function_tool(self.spawn_subagent),
                function_tool(self.list_subagents),
                function_tool(self.invoke_subagent),
                function_tool(self.wait_for_subagent_result),
                function_tool(self.wait_for_first_subagent_result),
                function_tool(self.delete_subagent),
                function_tool(self.delete_all),
                function_tool(self.finish),
            ],
        )
        self._session = SQLiteSession(session_id="orchestrator")

    async def run(self, prompt: str) -> str:
        """Run a single turn with the orchestrator.

        Emits events on `self.bus`; attaching a live display is the caller's job
        (see cli.py). The orchestrator no longer knows about any display.
        """
        result = await Runner.run(
            self._agent,
            prompt,
            max_turns=200,
            session=self._session,
        )
        return result.final_output or ""

    async def run_streamed(self, prompt: str) -> AsyncGenerator[StreamChunk, None]:
        """Stream orchestrator events in real time.

        Yields StreamChunk objects as the orchestrator thinks, calls tools,
        and receives tool results. The final summary arrives as a tool_call
        chunk with tool_name="finish".
        """
        streaming = Runner.run_streamed(
            self._agent,
            prompt,
            max_turns=200,
            session=self._session,
        )
        async for event in streaming.stream_events():
            if isinstance(event, RawResponsesStreamEvent):
                if isinstance(event.data, ResponseTextDeltaEvent):
                    yield StreamChunk(content=event.data.delta, type="text")
                elif isinstance(event.data, ResponseReasoningTextDeltaEvent):
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
                    text = "".join(
                        c.text for c in (item.raw_item.content or []) if hasattr(c, "text")
                    )
                    if text:
                        yield StreamChunk(content=text, type="thinking_done")
                elif isinstance(item, MessageOutputItem):
                    text = ItemHelpers.text_message_output(item)
                    if text:
                        yield StreamChunk(content=text, type="message_done")

    ## -----
    ## Tools

    def set_status(self, status: str) -> str:
        """Set a one-sentence status summary shown in the live display.
        Call this before every tool invocation unless the status is unchanged."""
        self.status = status
        self._bus.publish(ev.OrchestratorStatus(status=status))
        return "Status updated."

    def list_skills(self) -> str:
        """List all skills available to the orchestrator, with a one-line description each.
        Call this at the start of a session before deciding which skills to load."""
        skills = self._skills.list_skills()
        if not skills:
            return "No skills available."
        return "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)

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

        entry = await self._pool.create(agent_name, objective, extra_tools=extra_tools)
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

    async def invoke_subagent(self, agent_id: str, message: str) -> str:
        """Start a task on a subagent and return immediately (non-blocking).
        Use wait_for_subagent_result or wait_for_first_subagent_result to get the result."""
        entry = self._pool.get(agent_id)
        if entry is None:
            return f"Subagent '{agent_id}' not found. Use spawn_subagent first."
        if entry.run_task is not None and not entry.run_task.done():
            return f"Subagent '{agent_id}' is still running. Use wait_for_subagent_result to await its result."
        await entry.run(message)
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
        return result.final_output or ""

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

    def finish(self, summary: str) -> str:
        """Signal completion with a concise summary of what was accomplished,
        including key outputs, file paths, or results produced.
        Call this only after collecting all results you need from subagents.

        Arguments:
            summary: A concise summary of what was accomplished.
        """
        self.status = summary
        self._bus.publish(ev.OrchestratorStatus(status=summary))
        return summary
