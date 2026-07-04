"""Bash and filesystem tools, exercised against a temp workspace."""

from __future__ import annotations

from comet.tools import filesystem as fs
from comet.tools.bash import make_shell_tool
from comet.tools.context import ToolContext


async def test_shell_runs_in_workspace(tmp_path) -> None:
    seen: dict[str, str] = {}
    shell = make_shell_tool(
        ToolContext(workspace_dir=tmp_path, on_command=lambda c: seen.__setitem__("cmd", c))
    )
    out = await shell("echo hi && pwd")
    assert "hi" in out
    assert str(tmp_path) in out  # cwd is the workspace
    assert seen["cmd"] == "echo hi && pwd"


async def test_shell_blocks_dangerous(tmp_path) -> None:
    shell = make_shell_tool(ToolContext(workspace_dir=tmp_path))
    assert "blocked" in (await shell("sudo rm -rf /")).lower()


async def test_filesystem_roundtrip(tmp_path) -> None:
    ctx = ToolContext(workspace_dir=tmp_path)
    write, read, edit = fs.make_write_tool(ctx), fs.make_read_tool(ctx), fs.make_edit_tool(ctx)
    await write("a.txt", "hello world")
    assert "hello world" in await read("a.txt")
    await edit("a.txt", "hello", "goodbye")
    assert "goodbye world" in await read("a.txt")


async def test_filesystem_rejects_escape(tmp_path) -> None:
    read = fs.make_read_tool(ToolContext(workspace_dir=tmp_path))
    assert "escape" in (await read("../secret.txt")).lower()
