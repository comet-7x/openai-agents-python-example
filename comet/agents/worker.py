import asyncio
from pathlib import Path
from typing import Any

from agents import (
    Agent,
    AgentHooks,
    ModelSettings,
    OpenAIChatCompletionsModel,
    Tool,
    function_tool,
)
from agents.memory import SQLiteSession
from agents.run import Runner

from comet import config
from comet.message_bus import events as ev
from comet.message_bus.bus import MessageBus
from comet.tools.bash import make_shell_tool
from comet.tools.context import ToolContext

_WORKSPACES_ROOT = config.WORKSPACES_ROOT


SUBAGENT_BASE_INSTRUCTIONS = """\
You are a relentless executor. Your job is to complete the task fully - not to report that it's hard, \
not to ask for permission, not to stop because something wasn't pre-installed. Get it done.

## Default assumptions
- Writing and running code is expected. Do it.
- Installing dependencies, cloning repos, downloading data - this is normal setup work, not a blocker. Just do it.
- If something is unclear, make a reasonable assumption and proceed. Don't ask.
- If an approach fails, try another. Be creative. Persist.

## Status updates
Before every tool call, call set_status with a one-sentence description of what you are about to do, \
unless it is unchanged from the previous call.

## Execution
- Use the shell tool to write, run, and iterate on code.
- Prefer selectively viewing files (with `head` or `tail`) vs `cat` to save time and bandwidth.
- Prefer redirecting output to files or piping between commands, rather than viewing the output.
- Apply reasonable timeouts via shell constructs (e.g. `timeout 30 <cmd>`) to avoid hanging.
- If the same error recurs three times with no variation in approach, stop and report - don't loop blindly.

## Reporting
Your output goes directly into the orchestrator's context. Keep it tight:
- Report results only: file paths, metrics, outputs, errors.
- No process narration, no "I started by...".
- If genuinely blocked after exhausting options, state the blocker in one sentence.\
"""


class WorkerAgentHooks(AgentHooks):  # type: ignore[type-arg]
    def __init__(self) -> None:
        self._agent: WorkerAgent | None = None

    def bind(self, agent: "WorkerAgent") -> None:
        self._agent = agent

    async def on_llm_start(
        self, context: Any, agent: Any, system_prompt: Any, input_items: Any
    ) -> None:
        if self._agent is not None:
            self._agent.turns += 1
            self._agent._bus.publish(ev.SubagentTurn(id=self._agent.id))


class WorkerAgent:
    def __init__(
        self,
        id: str,
        name: str,
        model: OpenAIChatCompletionsModel,
        objective: str,
        workspace_dir: Path | None = None,
        extra_tools: list[Tool] | None = None,
        bus: MessageBus | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.gpu: str | None = None  # always None; kept for display compatibility
        self._bus = bus or MessageBus()

        # Shared per-session workspace: all subagents in a session use the same
        # directory, so a deliverable one writes (e.g. report.md) is visible to
        # every other subagent — including a later one asked to modify it. Falls
        # back to a private per-id dir when no session workspace is supplied.
        self.workspace_dir = workspace_dir if workspace_dir is not None else _WORKSPACES_ROOT / id
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        self.conv_session = SQLiteSession(session_id=f"{id}_conv")
        self.run_task: asyncio.Task[Any] | None = None

        self.turns = 0
        self.last_command: str | None = None
        self.status: str | None = None

        hooks = WorkerAgentHooks()

        def set_status(status: str) -> str:
            """Set a one-sentence status summary shown in the live display.
            Call this before every tool invocation unless the status is unchanged."""
            self.status = status
            self._bus.publish(ev.SubagentStatus(id=self.id, status=status))
            return "Status updated."

        def _record_command(cmd: str) -> None:
            self.last_command = cmd
            self._bus.publish(ev.SubagentCommand(id=self.id, command=cmd))

        shell = make_shell_tool(
            ToolContext(workspace_dir=self.workspace_dir, on_command=_record_command)
        )

        hooks.bind(self)  # bind before passing so the agent sees the ref immediately
        self.agent = Agent(
            name=name,
            model=model,
            instructions=(
                f"{SUBAGENT_BASE_INSTRUCTIONS}\n\n{objective}\n\n"
                f"## Working directory\n"
                f"Your shell commands run in {self.workspace_dir.resolve()}. "
                f"Relative paths resolve there - write all output files into it.\n"
                f"This directory is shared with the other subagents in this session and "
                f"persists across turns: files here (e.g. an earlier report) may already "
                f"exist for you to read or modify, and what you write is visible to others. "
                f"Check for existing files before assuming a clean slate; use distinct "
                f"filenames for your own intermediate scratch to avoid clobbering siblings."
            ),
            tools=[
                function_tool(set_status),
                function_tool(shell),
                *(extra_tools or []),
            ],
            model_settings=ModelSettings(tool_choice="required"),
            hooks=hooks,
        )

    @property
    def state(self) -> str:
        if self.run_task is None:
            return "idle"
        if not self.run_task.done():
            return "running"
        if self.run_task.exception() is not None:
            return "error"
        return "done"

    def text_summary(self) -> str:
        lines = [
            f"ID: {self.id}",
            f"Name: {self.name}",
            f"State: {self.state}",
            f"Turns: {self.turns}",
        ]
        if self.status:
            lines.append(f"Status: {self.status}")
        if self.last_command:
            lines.append(f"Last command: {self.last_command}")
        return "  ".join(lines)

    async def run(self, prompt: str) -> None:
        """Start a background task to run the worker agent (non-blocking)."""
        self.run_task = asyncio.create_task(
            Runner.run(
                self.agent,
                prompt,
                max_turns=200,
                session=self.conv_session,
            )
        )
        self._bus.publish(ev.SubagentState(id=self.id, state="running"))

        def _on_done(task: asyncio.Task[Any]) -> None:
            if task.cancelled():
                return
            state = "error" if task.exception() is not None else "done"
            self._bus.publish(ev.SubagentState(id=self.id, state=state))

        self.run_task.add_done_callback(_on_done)
