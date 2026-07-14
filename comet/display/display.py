from __future__ import annotations

import asyncio
import time

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from comet.display.view import SystemView

_SPINNER_FRAMES = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]


def format_duration(seconds: float) -> str:
    """Human-readable elapsed time with rollover: ``45s`` → ``1m 23s`` → ``1h 05m``.

    Used for the settled per-turn usage summary; see `format_clock` for the
    live ticking variant."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def format_clock(seconds: float) -> str:
    """Fixed-width ticking clock for the live panel: ``00:42`` → ``1:05:03``.

    Rolls over into an hours field past 60 minutes; the leading ``mm:ss`` stays
    zero-padded so the panel title doesn't jitter as it ticks."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class LiveDisplay:
    """Live display of orchestrator + subagent state.

    Push-based renderer of a `SystemView` (which the composition root keeps fed
    from the message bus). It holds no reference to any agent, which is what
    keeps the display decoupled from `comet.agents`.
    """

    def __init__(self, view: SystemView) -> None:
        self._view = view
        self._frame = 0
        self._start = time.perf_counter()  # for the live elapsed clock in the title
        self._live = Live(auto_refresh=False, transient=False)
        self._task: asyncio.Task[None] | None = None

    def _render_orchestrator(self) -> Panel:
        orch = self._view.orchestrator
        tool = orch.current_tool or "-"
        turns = orch.turns
        active = orch.phase != "idle"
        phase_label = {
            "waiting": "Waiting",
            "thinking": "Thinking",
            "responding": "Responding",
            "calling_tool": "Calling tool",
            "idle": "Idle",
        }.get(orch.phase, orch.phase)

        spinner = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)] if active else " "

        stats = Text()
        stats.append(f"{spinner} ", style="yellow" if active else "dim")
        stats.append(phase_label, style="yellow" if active else "dim")
        stats.append("    Turns: ", style="dim")
        stats.append(str(turns))
        stats.append("    Tool: ", style="dim")
        stats.append(tool, style="yellow" if orch.current_tool else "")

        lines: list[Text] = [stats]

        if orch.loaded_skills:
            skills_line = Text("\n")
            skills_line.append("Skills: ", style="dim")
            skills_line.append(", ".join(orch.loaded_skills), style="cyan")
            lines.append(skills_line)

        if orch.status:
            status_line = Text("\n")
            status_line.append("Status: ", style="dim")
            status_line.append(orch.status, style="italic")
            lines.append(status_line)

        u = self._view.usage
        if u.runs:
            usage_line = Text("\n")
            usage_line.append("Tokens: ", style="dim")
            usage_line.append(f"{u.total_tokens:,}")
            usage_line.append(f" (in {u.input_tokens:,} / out {u.output_tokens:,})", style="dim")
            usage_line.append("    Reqs: ", style="dim")
            usage_line.append(str(u.requests))
            lines.append(usage_line)

        content: Text | object = Text.assemble(*lines)

        clock = format_clock(time.perf_counter() - self._start)
        title = Text.assemble(Text("Orchestrator"), Text(f"  ·  {clock}", style="dim"))
        return Panel(content, title=title, border_style="dim")

    def _render_subagents(self) -> Panel:
        entries = self._view.subagent_list()
        table = Table(box=None, show_header=True, padding=(0, 2), header_style="dim", expand=True)
        table.add_column("", width=2)
        table.add_column("Agent")
        table.add_column("State", width=9)
        table.add_column("GPU", width=6)
        table.add_column("Turns", width=6)
        table.add_column("Status", ratio=1, no_wrap=True, overflow="ellipsis")

        state_styles: dict[str, tuple[str, str]] = {
            "running": ("yellow", _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]),
            "done": ("green", "✓"),
            "error": ("red", "✗"),
            "idle": ("dim", "·"),
        }
        for agent in entries:
            style, icon = state_styles.get(agent.state, ("", "?"))
            # A streaming subagent's live token tail takes over the status cell until the
            # turn settles; otherwise show its status or last command.
            if agent.output_tail:
                status_cell = Text(agent.output_tail.replace("\n", " "), style="dim")
            else:
                status_cell = Text(
                    agent.status or agent.last_command or "",
                    style="italic" if agent.status else "dim italic",
                )
            table.add_row(
                Text(icon, style=style),
                Text(agent.name),
                Text(f"[{agent.state}]", style=style),
                Text(agent.gpu or "", style="dim") if agent.gpu else Text(""),
                Text(str(agent.turns), style="dim") if agent.turns else Text(""),
                status_cell,
            )

        count = len(entries)
        title = f"Subagents - {count} active" if count else "Subagents - idle"
        return Panel(table, title=title, border_style="dim")

    def _render(self) -> Group:
        return Group(self._render_orchestrator(), self._render_subagents())

    async def _loop(self) -> None:
        while True:
            self._live.update(self._render(), refresh=True)
            self._frame += 1
            await asyncio.sleep(0.25)

    def start(self) -> None:
        print()
        self._live.start()
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._live.stop()
        print()
