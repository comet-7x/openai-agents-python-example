"""Bash and filesystem tools, exercised against a temp workspace."""

from __future__ import annotations

from comet.tools import filesystem as fs
from comet.tools.bash import check_workspace_escape, make_shell_tool
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


def test_workspace_escape_guard(tmp_path) -> None:
    root = tmp_path.resolve()
    # Blocked: navigating / writing / deleting outside the workspace.
    assert check_workspace_escape("cd /etc && ls", root)
    assert check_workspace_escape("echo x > /tmp/evil.txt", root)
    assert check_workspace_escape("rm -rf /tmp/evil", root)
    assert check_workspace_escape("cat foo > ../outside.txt", root)
    # Allowed: reads outside, installs/clones, and everything inside the workspace.
    assert check_workspace_escape("cat /etc/hostname", root) is None
    assert check_workspace_escape("pip install requests", root) is None
    assert check_workspace_escape("git clone https://example.com/r.git repo", root) is None
    assert check_workspace_escape("echo x > out.txt 2>&1", root) is None
    assert check_workspace_escape("mkdir sub && cd sub && pwd", root) is None
    assert check_workspace_escape("rm -rf ./build", root) is None


async def test_shell_blocks_workspace_escape(tmp_path) -> None:
    shell = make_shell_tool(ToolContext(workspace_dir=tmp_path))
    out = await shell("echo pwned > /tmp/escape_probe.txt")
    assert "blocked" in out.lower()
    # An in-workspace write of the same shape is allowed and actually runs.
    assert "exit_code: 0" in await shell("echo ok > inside.txt")
    assert (tmp_path / "inside.txt").exists()


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
