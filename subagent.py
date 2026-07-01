import asyncio
from typing import Any

from agents import Agent, AgentHooks, ModelSettings, OpenAIChatCompletionsModel, function_tool
from agents.memory import SQLiteSession
from agents.run import Runner


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


class SubAgentHooks(AgentHooks):  # type: ignore[type-arg]
    def __init__(self) -> None:
        self._agent: "SubAgent | None" = None

    def bind(self, agent: "SubAgent") -> None:
        self._agent = agent

    async def on_llm_start(self, context: Any, agent: Any, system_prompt: Any, input_items: Any) -> None:
        if self._agent is not None:
            self._agent.turns += 1


class SubAgent:
    def __init__(
        self,
        id: str,
        name: str,
        model: OpenAIChatCompletionsModel,
        objective: str,
    ) -> None:
        self.id = id
        self.name = name
        self.gpu: str | None = None  # always None; kept for display compatibility

        self.conv_session = SQLiteSession(session_id=f"{id}_conv")
        self.run_task: asyncio.Task[Any] | None = None

        self.turns = 0
        self.last_command: str | None = None
        self.status: str | None = None

        hooks = SubAgentHooks()

        def set_status(status: str) -> str:
            """Set a one-sentence status summary shown in the live display.
            Call this before every tool invocation unless the status is unchanged."""
            self.status = status
            return "Status updated."

        async def shell(command: str) -> str:
            """Execute a shell command locally and return its output (stdout + stderr + exit code)."""
            self.last_command = " ".join(command.splitlines()[:1])
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            return f"exit_code: {proc.returncode}\nstdout:\n{out}\nstderr:\n{err}"

        self.agent = Agent(
            name=name,
            model=model,
            instructions=f"{SUBAGENT_BASE_INSTRUCTIONS}\n\n{objective}",
            tools=[
                function_tool(set_status),
                function_tool(shell),
            ],
            model_settings=ModelSettings(tool_choice="required"),
        )
        hooks.bind(self)

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
        """Start a background task to run the subagent (non-blocking)."""
        self.run_task = asyncio.create_task(Runner.run(
            self.agent,
            prompt,
            max_turns=200,
            session=self.conv_session,
        ))
