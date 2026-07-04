"""Execution context passed to tool factories.

A tool factory (e.g. `make_shell_tool`) takes a `ToolContext` and returns the
actual tool coroutine. The context is what lets a tool operate on a specific
agent's workspace and report activity back, without the tool needing to import
or know about the agent/worker types.

Today `on_command` just updates the owning worker's `last_command` (which the
live display reads). In Phase 2 this reporting becomes a message-bus event and
this callback is replaced by an `emit` handle — the factory signature stays the
same, so tools won't need to change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolContext:
    workspace_dir: Path
    on_command: Callable[[str], None] | None = None
