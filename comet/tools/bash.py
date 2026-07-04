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
import signal
from dataclasses import dataclass, field

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

    async def shell(command: str) -> str:
        """Execute a shell command and return stdout, stderr, and exit code.

        Rejects commands matching dangerous patterns (privilege escalation,
        filesystem destruction, system control).  Kills the process after
        120 s.  Each output stream is capped at 50 000 characters.
        """
        # Report the command immediately — before any gate check — so the user
        # can see what is pending or running.
        if ctx.on_command is not None:
            ctx.on_command(" ".join(command.splitlines()[:1]))

        # Gate 1 — hard deny
        if any(pat in command for pat in _BLOCKED):
            return "Error: Dangerous command blocked."

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
