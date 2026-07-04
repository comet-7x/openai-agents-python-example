"""Filesystem tools — read / write / edit, scoped to an agent's workspace.

Factories mirror `make_shell_tool`: each takes a `ToolContext` and returns a
coroutine to wrap with `agents.function_tool`. Paths are resolved relative to
`ctx.workspace_dir` and confined to it, so a tool call cannot read or write
outside the agent's sandbox.

Not yet wired into the default worker toolset — established here so Phase 2 can
opt agents into them without another refactor.
"""

from __future__ import annotations

from pathlib import Path

from comet.tools.context import ToolContext

_READ_LIMIT = 50_000  # characters returned before truncation


def _resolve(ctx: ToolContext, rel_path: str) -> Path:
    """Resolve `rel_path` inside the workspace, rejecting escapes."""
    root = ctx.workspace_dir.resolve()
    target = (root / rel_path).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"path '{rel_path}' escapes the workspace")
    return target


def make_read_tool(ctx: ToolContext):
    async def read_file(path: str) -> str:
        """Read a UTF-8 text file from the workspace and return its contents."""
        try:
            target = _resolve(ctx, path)
        except ValueError as e:
            return f"Error: {e}"
        if not target.is_file():
            return f"Error: file not found: {path}"
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > _READ_LIMIT:
            return text[:_READ_LIMIT] + f"\n[… {len(text) - _READ_LIMIT} chars truncated]"
        return text

    return read_file


def make_write_tool(ctx: ToolContext):
    async def write_file(path: str, content: str) -> str:
        """Write `content` to a file in the workspace, creating parent dirs.
        Overwrites an existing file."""
        try:
            target = _resolve(ctx, path)
        except ValueError as e:
            return f"Error: {e}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {path}"

    return write_file


def make_edit_tool(ctx: ToolContext):
    async def edit_file(path: str, old: str, new: str) -> str:
        """Replace the first occurrence of `old` with `new` in a workspace file.
        Errors if `old` is absent or appears more than once."""
        try:
            target = _resolve(ctx, path)
        except ValueError as e:
            return f"Error: {e}"
        if not target.is_file():
            return f"Error: file not found: {path}"
        text = target.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return "Error: `old` string not found."
        if count > 1:
            return f"Error: `old` string is not unique ({count} matches)."
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"Edited {path}"

    return edit_file
