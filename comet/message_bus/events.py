"""Event types published on the message bus.

These dataclasses are the *contract* between event producers (agents) and
consumers (displays, loggers). They carry plain data only — no agent objects —
so a consumer never needs to import agent types. Every event has a `ts`; add
new event types here rather than overloading existing ones.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Event:
    ts: float = field(default_factory=time.time)


# ── Orchestrator ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OrchestratorTurn(Event):
    """The orchestrator started another LLM turn."""


@dataclass(frozen=True)
class OrchestratorThinking(Event):
    thinking: bool = False


@dataclass(frozen=True)
class OrchestratorTool(Event):
    tool: str | None = None  # tool name while running; None when the tool ends


@dataclass(frozen=True)
class OrchestratorStatus(Event):
    status: str = ""


@dataclass(frozen=True)
class OrchestratorSkills(Event):
    skills: tuple[str, ...] = ()


# ── Subagents ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SubagentSpawned(Event):
    id: str = ""
    name: str = ""
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubagentState(Event):
    id: str = ""
    state: Literal["idle", "running", "done", "error"] = "idle"


@dataclass(frozen=True)
class SubagentTurn(Event):
    id: str = ""


@dataclass(frozen=True)
class SubagentStatus(Event):
    id: str = ""
    status: str = ""


@dataclass(frozen=True)
class SubagentCommand(Event):
    id: str = ""
    command: str = ""


@dataclass(frozen=True)
class SubagentDeleted(Event):
    id: str = ""
