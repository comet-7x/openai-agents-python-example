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

# Orchestrator sub-phase within a turn (see OrchestratorPhase).
Phase = Literal["idle", "waiting", "thinking", "responding", "calling_tool"]


@dataclass(frozen=True)
class Event:
    ts: float = field(default_factory=time.time)


# ── Orchestrator ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OrchestratorTurn(Event):
    """The orchestrator started another LLM turn (one LLM call = one loop iteration)."""


@dataclass(frozen=True)
class OrchestratorPhase(Event):
    """Which sub-phase of a turn the orchestrator is in.

    - `idle`         — no LLM call in flight (between turns / run finished).
    - `waiting`      — request sent, awaiting the first token (time-to-first-token).
                       The hooks can only ever reach this on LLM start; the deltas
                       promote it further.
    - `thinking`     — the model is streaming reasoning (`reasoning_content`) tokens.
    - `responding`   — the model is streaming the visible answer.
    - `calling_tool` — a tool call is executing.

    `waiting`/`thinking`/`responding` are distinct on purpose: a non-reasoning model
    goes `waiting → responding` (no fake 'thinking'), a reasoning model goes
    `waiting → thinking → responding`. The hooks emit the coarse boundaries (so the
    non-streamed path still works); the streamed delta loop is the only place the
    reasoning→content transition is observable, so it emits `thinking`/`responding`."""

    phase: Phase = "idle"


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
class SubagentOutput(Event):
    """A token-level delta from a subagent's *own* LLM stream, published only when
    subagent streaming is enabled (`--stream-subagents`). `channel` separates
    reasoning from the visible answer. This is a display/observability signal — the
    parent's consumption is unchanged: it still uses the subagent's final result."""

    id: str = ""
    delta: str = ""
    channel: Literal["thinking", "text"] = "text"


@dataclass(frozen=True)
class SubagentDeleted(Event):
    id: str = ""


# ── Metrics ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class UsageReported(Event):
    """Token usage + wall time for one completed agent run — the orchestrator or
    a single subagent. Consumers sum these into a session total; the JSONL sink
    records each one, giving a per-agent cost audit in the run trace."""

    source: str = ""  # "orchestrator" or a subagent id
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    elapsed_s: float = 0.0

    @classmethod
    def from_usage(cls, source: str, usage: object, elapsed_s: float) -> UsageReported:
        """Build from the SDK's ``Usage`` (``context_wrapper.usage``). Duck-typed
        so this module stays a leaf — it never imports the agents SDK."""
        return cls(
            source=source,
            requests=getattr(usage, "requests", 0),
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            total_tokens=getattr(usage, "total_tokens", 0),
            elapsed_s=elapsed_s,
        )
