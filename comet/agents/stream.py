"""Streaming protocol shared by agents.

`StreamChunk` is the token/event unit an agent yields from a `run_streamed`
loop; displays consume it to render output in real time. It is agent-agnostic
on purpose — the orchestrator emits it today, and a worker that gains
token-level streaming would yield the same shape — so it lives here rather than
inside any one agent module (mirrors how ``message_bus/events.py`` holds the
event dataclasses).
"""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class StreamChunk:
    """A single streamed event from an agent.

    type values:
      "text"          — final-answer token delta (streamed, use to print in real time)
      "thinking"      — reasoning token delta (streamed; only fires on reasoning models)
      "tool_call"     — the agent called a tool (complete, not delta)
      "tool_output"   — tool returned a result (complete)
      "thinking_done" — full reasoning block finished (content = complete reasoning text)
      "message_done"  — the agent's current message is complete

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
