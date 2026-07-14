"""View-model for the live display, driven entirely by bus events.

`SystemView.apply(event)` is the reducer: subscribe it to a `MessageBus` and it
keeps an up-to-date picture of the orchestrator and subagents. Displays render
this view — they never read agent objects — which is what breaks the old
display→agents coupling.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from comet.message_bus import events as ev

# How many trailing chars of a streaming subagent's output to keep for the live
# row (the table cell is single-line/ellipsised, so this is just a rolling tail).
_OUTPUT_TAIL = 200


@dataclass
class OrchestratorView:
    turns: int = 0
    status: str | None = None
    current_tool: str | None = None
    loaded_skills: list[str] = field(default_factory=list)
    phase: str = "idle"  # idle | waiting | thinking | responding | calling_tool
    reasoning_effort: str | None = None  # reserved; kept for panel parity


@dataclass
class SubagentView:
    id: str
    name: str
    state: str = "idle"
    turns: int = 0
    status: str | None = None
    last_command: str | None = None
    output_tail: str = ""  # rolling tail of the current turn's streamed tokens
    gpu: str | None = None  # reserved; kept for panel parity


@dataclass
class UsageView:
    """Running session totals, summed from every `UsageReported` event.

    `agent_seconds` is *cumulative* per-run time across the orchestrator and all
    subagents — not wall-clock. Because subagents run concurrently it can exceed
    the actual elapsed time; the CLI measures wall-clock separately.
    """

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    runs: int = 0
    agent_seconds: float = 0.0


class SystemView:
    def __init__(self) -> None:
        self.orchestrator = OrchestratorView()
        # insertion-ordered: preserves spawn order for the subagent table
        self.subagents: dict[str, SubagentView] = {}
        self.usage = UsageView()

    def apply(self, event: ev.Event) -> None:
        o = self.orchestrator
        match event:
            case ev.OrchestratorTurn():
                o.turns += 1
            case ev.OrchestratorPhase(phase=p):
                o.phase = p
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
                    if state != "running":
                        sub.output_tail = ""  # settle the row once the run ends
            case ev.SubagentTurn(id=sid):
                if (sub := self.subagents.get(sid)) is not None:
                    sub.turns += 1
                    sub.output_tail = ""  # fresh tail per turn — show current activity
            case ev.SubagentOutput(id=sid, delta=delta):
                if (sub := self.subagents.get(sid)) is not None:
                    sub.output_tail = (sub.output_tail + delta)[-_OUTPUT_TAIL:]
            case ev.SubagentStatus(id=sid, status=s):
                if (sub := self.subagents.get(sid)) is not None:
                    sub.status = s
            case ev.SubagentCommand(id=sid, command=cmd):
                if (sub := self.subagents.get(sid)) is not None:
                    sub.last_command = cmd
            case ev.SubagentDeleted(id=sid):
                self.subagents.pop(sid, None)
            case ev.UsageReported() as u:
                self.usage.requests += u.requests
                self.usage.input_tokens += u.input_tokens
                self.usage.output_tokens += u.output_tokens
                self.usage.total_tokens += u.total_tokens
                self.usage.agent_seconds += u.elapsed_s
                self.usage.runs += 1

    def subagent_list(self) -> list[SubagentView]:
        return list(self.subagents.values())
