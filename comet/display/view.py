"""View-model for the live display, driven entirely by bus events.

`SystemView.apply(event)` is the reducer: subscribe it to a `MessageBus` and it
keeps an up-to-date picture of the orchestrator and subagents. Displays render
this view — they never read agent objects — which is what breaks the old
display→agents coupling.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from comet.message_bus import events as ev


@dataclass
class OrchestratorView:
    turns: int = 0
    status: str | None = None
    current_tool: str | None = None
    loaded_skills: list[str] = field(default_factory=list)
    is_thinking: bool = False
    reasoning_effort: str | None = None  # reserved; kept for panel parity


@dataclass
class SubagentView:
    id: str
    name: str
    state: str = "idle"
    turns: int = 0
    status: str | None = None
    last_command: str | None = None
    gpu: str | None = None  # reserved; kept for panel parity


class SystemView:
    def __init__(self) -> None:
        self.orchestrator = OrchestratorView()
        # insertion-ordered: preserves spawn order for the subagent table
        self.subagents: dict[str, SubagentView] = {}

    def apply(self, event: ev.Event) -> None:
        o = self.orchestrator
        match event:
            case ev.OrchestratorTurn():
                o.turns += 1
            case ev.OrchestratorThinking(thinking=t):
                o.is_thinking = t
            case ev.OrchestratorTool(tool=tool):
                o.current_tool = tool
            case ev.OrchestratorStatus(status=s):
                o.status = s
            case ev.OrchestratorSkills(skills=skills):
                o.loaded_skills = list(skills)
            case ev.SubagentSpawned(id=sid, name=name):
                self.subagents[sid] = SubagentView(id=sid, name=name, state="idle")
            case ev.SubagentState(id=sid, state=state):
                if (sub := self.subagents.get(sid)) is not None:
                    sub.state = state
            case ev.SubagentTurn(id=sid):
                if (sub := self.subagents.get(sid)) is not None:
                    sub.turns += 1
            case ev.SubagentStatus(id=sid, status=s):
                if (sub := self.subagents.get(sid)) is not None:
                    sub.status = s
            case ev.SubagentCommand(id=sid, command=cmd):
                if (sub := self.subagents.get(sid)) is not None:
                    sub.last_command = cmd
            case ev.SubagentDeleted(id=sid):
                self.subagents.pop(sid, None)

    def subagent_list(self) -> list[SubagentView]:
        return list(self.subagents.values())
