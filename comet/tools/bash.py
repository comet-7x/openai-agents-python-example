"""The shell/bash tool — extracted from WorkerAgent so any agent can use it.

`make_shell_tool(ctx)` returns an async `shell(command)` coroutine bound to the
given `ToolContext` (its workspace directory and command-reporting callback).
Wrap the returned coroutine with `agents.function_tool` to expose it to an agent.

Command safety is enforced in three gates:
  1. hard deny  — always blocked, no override (`_BLOCKED`)
  2. warn       — paused for human approval (`_WARN_PATTERNS`)
  3. approval   — resolved via an async queue wired up by the interactive UI
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path

from comet.tools.context import ToolContext

# ── Gate 1: hard deny — always blocked, no override ───────────────────────────
_BLOCKED = [
    "rm -rf /",  # root wipe
    "rm -rf /*",  # root wipe variant
    "sudo",  # privilege escalation
    "shutdown",  # system control
    "reboot",
    "halt",
    "mkfs",  # filesystem format
    "> /dev/",  # raw device write
    "dd if=",  # disk image write
]

# ── Gate 2: warn patterns — paused for human approval ─────────────────────────
# Each entry is (substring_to_match, human_readable_reason).
_WARN_PATTERNS: list[tuple[str, str]] = [
    ("rm ", "deletes files"),
    ("chmod 777", "makes files world-writable"),
    ("> /etc/", "writes to system config"),
    ("| bash", "pipes network content to bash"),
    ("| sh", "pipes network content to sh"),
]

_SHELL_TIMEOUT = 120  # seconds before the process is killed
_OUTPUT_LIMIT = 50_000  # characters per stream before truncation


# ── Workspace-escape guard ─────────────────────────────────────────────────────
# A soft confinement: subagents run with their cwd set to the session workspace,
# but nothing stops a shell command from navigating or writing elsewhere. These
# checks block the common ways a command leaves the workspace to *modify* things —
# cd-ing out, redirecting output to an outside file, or rm-ing an outside path —
# while deliberately leaving reads (cat /etc/hosts) and package installs / clones
# (which write to system dirs) alone. Substring/regex-based, so not airtight; it
# catches accidents and casual escapes, not a determined adversary.
_CD_RE = re.compile(r"(?:^|[\s;&|(])cd\s+([^\s;&|<>]+)")
_REDIRECT_RE = re.compile(r"\d*>>?\s*(?!&)([^\s;|&<>]+)")
_RM_RE = re.compile(r"(?:^|[\s;&|(])rm\s+([^;&|<>]+)")


def _resolves_outside(target: str, root: Path) -> bool:
    """True if `target` (a shell path token) resolves outside `root`. Absolute
    paths and ~ are honored; relative paths resolve against the workspace root."""
    target = target.strip().strip("\"'")
    if not target or target.startswith("&"):
        return False
    expanded = os.path.expanduser(target)
    p = Path(expanded)
    if not p.is_absolute():
        p = root / p
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError):  # e.g. a symlink loop
        return False
    return resolved != root and root not in resolved.parents


def check_workspace_escape(command: str, root: Path) -> str | None:
    """Return a human-readable reason if `command` would leave `root` to modify
    something, else None. Reads outside the workspace are allowed."""
    for m in _CD_RE.finditer(command):
        target = m.group(1)
        if target == "-":  # `cd -` (previous dir) — don't second-guess it
            continue
        if _resolves_outside(target, root):
            return f"'cd {target}' leaves the workspace"
    for m in _REDIRECT_RE.finditer(command):
        if _resolves_outside(m.group(1), root):
            return f"redirect to '{m.group(1)}' writes outside the workspace"
    for m in _RM_RE.finditer(command):
        for arg in m.group(1).split():
            if not arg.startswith("-") and _resolves_outside(arg, root):
                return f"'rm' targets '{arg}' outside the workspace"
    return None


# ── Gate 3: approval channel ───────────────────────────────────────────────────
@dataclass
class ApprovalRequest:
    """Posted to the approval queue when Gate 2 triggers.

    The shell tool awaits ``event`` until the main loop resolves the request
    (sets ``approved`` and fires ``event``).
    """

    command: str
    approved: bool = field(default=False, init=False)
    event: asyncio.Event = field(default_factory=asyncio.Event, init=False)


_approval_queue: asyncio.Queue[ApprovalRequest] | None = None


def set_approval_queue(q: asyncio.Queue[ApprovalRequest] | None) -> None:
    """Called by the interactive UI to wire up the approval channel."""
    global _approval_queue
    _approval_queue = q


def make_shell_tool(ctx: ToolContext):
    """Build a shell tool bound to `ctx`. Wrap the result with function_tool."""

    workspace_root = ctx.workspace_dir.resolve()

    async def shell(command: str) -> str:
        """Execute a shell command and return stdout, stderr, and exit code.

        Rejects commands matching dangerous patterns (privilege escalation,
        filesystem destruction, system control) and commands that leave the
        workspace to modify things (cd/redirect/rm outside it).  Kills the
        process after 120 s.  Each output stream is capped at 50 000 characters.
        """
        # Report the command immediately — before any gate check — so the user
        # can see what is pending or running.
        if ctx.on_command is not None:
            ctx.on_command(" ".join(command.splitlines()[:1]))

        # Gate 1 — hard deny
        if any(pat in command for pat in _BLOCKED):
            return "Error: Dangerous command blocked."

        # Gate 1b — workspace confinement (soft): block writes/navigation that
        # leave the workspace; reads and package installs/clones are unaffected.
        escape_reason = check_workspace_escape(command, workspace_root)
        if escape_reason:
            return (
                f"Blocked: {escape_reason}. Keep your files inside the workspace "
                f"({workspace_root}) and use workspace-relative paths."
            )

        # Gate 2 — warn patterns → Gate 3 (human approval)
        warn_reason = next(
            (reason for pat, reason in _WARN_PATTERNS if pat in command),
            None,
        )
        if warn_reason:
            if _approval_queue is None:
                # Non-interactive: no human available — auto-deny for safety.
                return (
                    f"Blocked: '{warn_reason}' — command requires human approval "
                    f"but the session is non-interactive."
                )
            req = ApprovalRequest(command=command)
            await _approval_queue.put(req)
            await req.event.wait()  # blocks until main loop resolves
            if not req.approved:
                return f"Denied: user rejected the command ({warn_reason})."

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,  # never let the child touch our real tty
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group, so we can kill descendants too
                cwd=ctx.workspace_dir,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=_SHELL_TIMEOUT
                )
            except TimeoutError:
                # Killing just the top-level shell leaves orphaned children (background
                # jobs, subshells, an interactive REPL launched with no args) holding the
                # stdout/stderr pipes open — communicate() would then hang forever. Kill
                # the whole process group, and bound the reap in case something still lingers.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.communicate(), timeout=5)
                return f"Error: Timeout ({_SHELL_TIMEOUT} s) — process killed."
        except OSError as e:
            return f"Error: {e}"

        out = stdout_b.decode("utf-8", errors="replace")
        err = stderr_b.decode("utf-8", errors="replace")

        if len(out) > _OUTPUT_LIMIT:
            out = out[:_OUTPUT_LIMIT] + f"\n[… {len(out) - _OUTPUT_LIMIT} chars truncated]"
        if len(err) > _OUTPUT_LIMIT:
            err = err[:_OUTPUT_LIMIT] + f"\n[… {len(err) - _OUTPUT_LIMIT} chars truncated]"

        return (
            f"exit_code: {proc.returncode}\n"
            f"stdout:\n{out.strip() or '(no output)'}\n"
            f"stderr:\n{err.strip() or '(none)'}"
        )

    return shell
