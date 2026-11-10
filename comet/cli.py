import argparse
import asyncio
import os
import sys
import time

# Running this file directly (`python comet/cli.py`) puts the comet/ directory on
# sys.path[0], which shadows the third-party `agents` package with our own
# comet/agents/ — so `import agents` fails. Swap in the repo root so both resolve.
# No-op under `python -m comet.cli` or the installed `comet` console script.
if os.path.basename(sys.path[0] or "") == "comet":
    sys.path[0] = os.path.dirname(sys.path[0])

from agents import set_tracing_disabled
from openai import APIConnectionError, AsyncOpenAI
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import comet.tools.bash as _bash
from comet import config
from comet.agents.orchestrator import OrchestratorAgent
from comet.agents.stream import StreamChunk
from comet.display.display import LiveDisplay, format_clock, format_duration
from comet.display.view import SystemView
from comet.llm.models import ConcurrencyLimitedModel
from comet.message_bus.bus import MessageBus
from comet.message_bus.sinks.jsonl_logger import JsonlLogger

set_tracing_disabled(disabled=True)

_client = AsyncOpenAI(
    base_url=config.LLM_BASE_URL,
    api_key=config.LLM_API_KEY,
    timeout=config.LLM_REQUEST_TIMEOUT,
    max_retries=1,
)

_model = ConcurrencyLimitedModel(
    max_concurrency=config.LLM_MAX_CONCURRENCY,
    model=config.LLM_MODEL,
    openai_client=_client,
)

_SPINNER_FRAMES = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]


class StreamUI:
    """Terminal UI for streaming mode.

    Uses Rich Live with auto_refresh=False so the spinner is driven by an
    asyncio task in the same event loop — no background thread, no race
    condition with console.print().
    """

    _BULLET = "⏺"
    _ARROW_IN = "↩"
    _KEY_WIDTH = 18
    _VAL_WIDTH = 120
    _REASONING_WIDTH = 72  # rolling tail of live reasoning shown in the spinner line
    _transient = True  # subclasses set False to keep the Live area visible on stop

    def __init__(self) -> None:
        self._console = Console(highlight=False)
        self._frame = 0
        # "Client…" = request in flight, no token yet (time-to-first-token); it
        # becomes "Reasoning…" if reasoning deltas arrive, or the answer text itself
        # once content streams. Mirrors the OrchestratorPhase waiting/thinking split.
        self._status_msg = "Client…"
        self._pending: dict[str, str] = {}  # call_id → tool_name
        self._last_char = "\n"
        self._text_buf = ""  # partial line shown inside the Live area
        self._thinking = False  # True while receiving "thinking" deltas
        self._reasoning_buf = ""  # accumulated reasoning text for the live tail preview
        # _make_renderable() reads _text_buf/_status_msg, so init those first
        self._live = Live(
            self._make_renderable(),
            console=self._console,
            auto_refresh=False,  # driven by asyncio task — no background thread
            transient=self._transient,
        )

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def __enter__(self) -> "StreamUI":
        self._live.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._flush_text()
        # Render the final state before stop.  _make_final_renderable() lets
        # subclasses control what stays visible: the base class clears the
        # spinner (transient=True removes it anyway); StreamDisplay shows only
        # the status panels (no spinning indicator).
        self._live.update(self._make_final_renderable(), refresh=True)
        self._live.stop()

    def _make_final_renderable(self) -> RenderableType:
        """Renderable committed to the terminal when the Live context exits.

        Override in subclasses to control what remains visible after stop.
        Default: empty — with transient=True the line is cleared entirely.
        """
        return Text("")

    def tick(self) -> None:
        """Advance one spinner frame — called by the asyncio spinner task."""
        self._frame += 1
        self._live.update(self._make_renderable(), refresh=True)

    # ── public ─────────────────────────────────────────────────────────────────

    def on_chunk(self, chunk: StreamChunk) -> None:
        if chunk.type == "text":
            # Suppress pure-whitespace tokens; the model emits "\n\n" between
            # tool calls and those would print as spurious blank lines.
            if not chunk.content.strip():
                return
            # Reasoning that streamed but never got a thinking_done (it arrives
            # late, after text starts, on some backends) is committed here before
            # the answer so it isn't lost or printed out of order.
            if self._thinking:
                self._commit_reasoning(self._reasoning_buf)
                self._thinking = False
                self._reasoning_buf = ""
            self._set_waiting()
            self._write_text(chunk.content)

        elif chunk.type == "thinking":
            # Reasoning token delta — accumulate and show the rolling tail live in
            # the spinner line (not a growing block), so you watch the model think
            # in real time. A one-line record is committed to scrollback on done.
            if not self._thinking:
                self._thinking = True
                self._reasoning_buf = ""
            self._reasoning_buf += chunk.content
            self._set_reasoning()

        elif chunk.type == "thinking_done":
            # Commit the reasoning as one dim line — but only while still
            # mid-reasoning. A late thinking_done (after text already started and
            # committed the reasoning) is a duplicate and dropped.
            if self._thinking:
                self._commit_reasoning(chunk.content or self._reasoning_buf)
                self._thinking = False
                self._reasoning_buf = ""
                self._set_waiting()

        elif chunk.type == "tool_call":
            self._render_tool_call(chunk)

        elif chunk.type == "tool_output":
            self._render_tool_output(chunk)

        elif chunk.type == "message_done":
            self._flush_text()

    # ── private ────────────────────────────────────────────────────────────────

    def _make_renderable(self) -> RenderableType:
        """Live area: partial text line OR spinner — never both.

        While text is streaming the growing line is the progress indicator;
        the spinner only appears when waiting (between events, tool calls, etc).
        """
        if self._text_buf:
            return Text(self._text_buf)
        frame = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
        return Text(f"{frame} {self._status_msg}", style="yellow dim")

    def _set_waiting(self) -> None:
        self._status_msg = "Waiting…"
        self._live.update(self._make_renderable(), refresh=True)

    def _set_reasoning(self) -> None:
        self._status_msg = "Reasoning…"
        self._live.update(self._make_renderable(), refresh=True)

    def _set_message_done(self) -> None:
        """Terminal state for a finished turn: the run completed (via finish() or a
        clean end of stream), so show 'Completed' instead of reverting to
        'Waiting…' as if another LLM call were imminent."""
        self._status_msg = "Completed"
        self._live.update(self._make_renderable(), refresh=True)

    def _commit_reasoning(self, text: str) -> None:
        """Print the finished reasoning as one dim line above the Live area — a
        permanent record in scrollback, since the live tail in the spinner is
        ephemeral and vanishes once the answer or next tool call starts."""
        line = " ".join(text.split())
        if not line:
            return
        if len(line) > self._VAL_WIDTH:
            line = line[: self._VAL_WIDTH - 1] + "…"
        self._flush_text()
        self._live.console.print(f"[dim italic]◌ Reasoning · {line}[/dim italic]")
        self._last_char = "\n"

    def _flush_text(self) -> None:
        """Print any remaining buffered text and ensure we end on a new line."""
        if self._text_buf:
            content = self._text_buf
            # Clear _text_buf and update the stored renderable BEFORE printing.
            # live.console.print() re-renders the Live area on the way out;
            # if _text_buf still has content at that moment the text appears
            # both in the printed line above AND in the Live stream_line.
            self._text_buf = ""
            self._live.update(self._make_renderable())  # no terminal refresh yet
            self._live.console.print(content)  # prints above; re-renders cleanly
            self._last_char = "\n"
        elif self._last_char != "\n":
            self._live.console.print()
            self._last_char = "\n"

    def _write_text(self, content: str) -> None:
        """Buffer text, print complete lines above the Live area, and show the
        partial line inside the Live renderable for real-time streaming feel.

        Rich Live requires the cursor to be at the start of its own line — we
        never call console.print(end=""). Instead the incomplete fragment lives
        inside _make_renderable() and is refreshed on every tick/token.
        """
        self._text_buf += content
        if "\n" in self._text_buf:
            lines = self._text_buf.split("\n")
            complete, tail = lines[:-1], lines[-1]
            # Move _text_buf to just the tail and update the stored renderable
            # BEFORE printing any complete line.  Same reason as _flush_text:
            # live.console.print() re-renders on exit and must not see the
            # lines we're about to move above the Live area.
            self._text_buf = tail
            self._live.update(self._make_renderable())  # no terminal refresh yet
            for line in complete:
                if line == "" and self._last_char == "\n":
                    continue
                self._live.console.print(line)
                self._last_char = "\n"
        # Refresh so the partial line (tail) appears immediately in the Live area
        self._live.update(self._make_renderable(), refresh=True)

    def _fmt_value(self, v: object) -> str:
        s = v if isinstance(v, str) else repr(v)
        s = str(s).replace("\n", " ")
        if len(s) > self._VAL_WIDTH:
            s = s[: self._VAL_WIDTH - 3] + "…"
        return s

    def _render_tool_call(self, chunk: StreamChunk) -> None:
        tool = chunk.tool_name or "?"
        args = chunk.tool_args or {}

        if chunk.tool_call_id:
            self._pending[chunk.tool_call_id] = tool

        self._flush_text()  # flush any partial text before the tool block
        self._live.console.print()  # blank separator line
        self._last_char = "\n"

        if tool == "finish":
            # finish() ends the run via the orchestrator's finish-gate (once a clean
            # verify() has passed); on that terminal call the model emits no further
            # "text", so its summary IS the final answer. Show it in full, not
            # truncated like a regular tool arg. (A rare gate-refused finish — called
            # before verifying — renders here too, then the run continues.)
            self._live.console.print(Text("Answer", style="bold green"))
            summary = args.get("summary")
            if isinstance(summary, str) and summary.strip():
                self._live.console.print(summary.strip())
                self._last_char = "\n"
            return

        is_status = tool == "set_status"
        bullet_style = "dim" if is_status else "bold cyan"
        name_style = "dim" if is_status else "bold"

        self._live.console.print(
            Text.assemble(
                Text(f"{self._BULLET} ", style=bullet_style),
                Text(tool, style=name_style),
            )
        )
        for k, v in args.items():
            self._live.console.print(
                Text.assemble(
                    Text(f"  {k:<{self._KEY_WIDTH}}", style="dim"),
                    Text(self._fmt_value(v), style="dim" if is_status else ""),
                )
            )

    def _render_tool_output(self, chunk: StreamChunk) -> None:
        call_id = chunk.tool_call_id
        tool_name = self._pending.pop(call_id, None) if call_id else None

        if tool_name == "finish":
            # finish() ends the run via the finish-gate — this is the turn's completion.
            self._set_message_done()
            return
        if tool_name == "set_status":
            self._set_waiting()
            return

        content = chunk.content.strip().replace("\n", " ")
        if len(content) > self._VAL_WIDTH:
            content = content[: self._VAL_WIDTH - 3] + "…"

        self._live.console.print(f"  [dim]{self._ARROW_IN} {content}[/dim]")
        self._last_char = "\n"
        self._set_waiting()


class RobustStreamUI(StreamUI):
    """StreamUI subclass that handles common model streaming quirks.

    1. thinking / thinking_done
              — reasoning streams live as a rolling one-line tail in the spinner
                (see the base class), committed to scrollback as one dim line when
                it settles. Handles the two ways reasoning ends: an in-order
                thinking_done (reasoning → tool call), and a late thinking_done that
                arrives *after* text already started (RunItemStreamEvent fires after
                the raw deltas) — the text branch commits the reasoning first and the
                late duplicate is dropped via the `_thinking` guard.
    2. text   — skips leading whitespace tokens before real content starts;
                prints an "Answer" header on the first real text token.
    3. message_done
              — only flushed/acknowledged when content is non-empty, avoiding
                spurious blank output from empty MessageOutputItems.
    4. tool_call / tool_output
              — resets _text_started so an "Answer" header can be re-emitted
                if the model outputs text again after tool results.
    """

    def __init__(self) -> None:
        super().__init__()
        self._text_started: bool = False
        self._answer_shown: bool = False  # print "Answer" header only once per run

    def on_chunk(self, chunk: StreamChunk) -> None:
        t = chunk.type

        if t == "thinking":
            # Accumulate and show the rolling tail live in the spinner line; the
            # base helpers keep this to one line and commit a record on done.
            if not self._thinking:
                self._flush_text()
                self._thinking = True
                self._reasoning_buf = ""
            self._reasoning_buf += chunk.content
            self._set_reasoning()

        elif t == "thinking_done":
            # Commit only while still mid-reasoning; a late thinking_done (after
            # text already committed the reasoning) is a duplicate and dropped.
            if self._thinking:
                self._commit_reasoning(chunk.content or self._reasoning_buf)
                self._thinking = False
                self._reasoning_buf = ""
                self._set_waiting()

        elif t == "text":
            # Leading whitespace before any real content — discard silently.
            if not self._text_started and not chunk.content.strip():
                return
            if not self._text_started:
                self._flush_text()
                # thinking → text: no thinking_done has arrived yet (it comes late),
                # so commit the streamed reasoning here before the answer begins.
                if self._thinking:
                    self._commit_reasoning(self._reasoning_buf)
                    self._thinking = False
                    self._reasoning_buf = ""
                    self._set_waiting()
                self._live.console.print()
                if not self._answer_shown:
                    self._live.console.print("[bold]Answer[/bold]")
                    self._answer_shown = True
                self._last_char = "\n"
                self._text_started = True
            self._write_text(chunk.content)

        elif t == "tool_call":
            self._text_started = False
            self._render_tool_call(chunk)

        elif t == "tool_output":
            self._text_started = False
            self._render_tool_output(chunk)

        elif t == "message_done":
            self._text_started = False
            # Empty MessageOutputItems (before tool calls) produce empty
            # message_done chunks — skip them to avoid spurious blank output.
            if chunk.content.strip():
                self._flush_text()


_STATE_STYLES: dict[str, tuple[str, str]] = {
    "running": ("yellow", "⣾"),
    "done": ("green", "✓"),
    "error": ("red", "✗"),
    "idle": ("dim", "·"),
}


class StreamDisplay(RobustStreamUI):
    """RobustStreamUI + live orchestrator/subagent panels (mirrors LiveDisplay).

    Set the view before calling super().__init__() so that _make_renderable() —
    invoked during Live construction — can read it. The view is fed from the
    message bus by the composition root; StreamDisplay only renders it.

    _transient = False keeps the panels visible after the run completes,
    matching the behaviour of the non-stream LiveDisplay.
    """

    _transient = False

    def __init__(self, view: SystemView) -> None:
        self._view = view  # must precede super().__init__()
        self._start = time.perf_counter()  # live elapsed clock; read in _render_orch_panel
        super().__init__()

    # ── override: include orchestrator + subagent panels in the Live area ──────

    def _make_renderable(self) -> RenderableType:
        # Spinner lives in the Orchestrator panel title — no separate top line.
        # Show the partial streaming line above the panels when text is arriving.
        items: list[RenderableType] = []
        if self._text_buf:
            items.append(Text(self._text_buf))
        items.append(self._render_orch_panel())
        items.append(self._render_sub_panel())
        return Group(*items)

    def _make_final_renderable(self) -> RenderableType:
        """On stop: show panels only — no streaming line, no spinner."""
        return Group(self._render_orch_panel(), self._render_sub_panel())

    def _render_orch_panel(self) -> Panel:
        orch = self._view.orchestrator
        frame = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]

        # Panel title carries the animated spinner + current status message + a
        # live elapsed clock, making it the prominent live indicator at the top of
        # the Live area. The clock ticks because tick() re-renders this each frame.
        clock = format_clock(time.perf_counter() - self._start)
        title = Text.assemble(
            Text(f"{frame}  {self._status_msg}", style="yellow dim")
            if self._status_msg != "Completed"
            else Text(f"✿  {self._status_msg}", style="blue dim"),
            Text(f"  ·  {clock}", style="dim"),
        )

        line = Text()
        line.append("Turns: ", style="dim")
        line.append(str(orch.turns))
        if orch.current_tool:
            line.append("    Tool: ", style="dim")
            line.append(orch.current_tool, style="yellow")
        if orch.loaded_skills:
            line.append("    Skills: ", style="dim")
            line.append(", ".join(orch.loaded_skills), style="cyan")
        if orch.status:
            line.append("\nStatus: ", style="dim")
            line.append(orch.status, style="italic")

        u = self._view.usage
        if u.runs:
            line.append("\nTokens: ", style="dim")
            line.append(f"{u.total_tokens:,}")
            line.append(f" (in {u.input_tokens:,} / out {u.output_tokens:,})", style="dim")
            line.append("   Reqs: ", style="dim")
            line.append(str(u.requests))

        return Panel(line, title=title, title_align="left", border_style="dim")

    def _render_sub_panel(self) -> Panel:
        entries = self._view.subagent_list()
        table = Table(box=None, show_header=True, padding=(0, 2), header_style="dim", expand=True)
        table.add_column("", width=2)
        table.add_column("Agent")
        table.add_column("State", width=9)
        table.add_column("Turns", width=5)
        table.add_column("Status", ratio=1, no_wrap=True, overflow="ellipsis")

        for e in entries:
            style, icon = _STATE_STYLES.get(e.state, ("", "?"))
            # While streaming, the live token tail takes over the status cell; once the
            # turn settles the tail clears and status/command shows again.
            if e.output_tail:
                status_cell = Text(e.output_tail.replace("\n", " "), style="dim")
            else:
                status_cell = Text(
                    "  ".join(
                        filter(
                            None,
                            [e.status, f"$ {e.last_command}" if e.last_command else None],
                        )
                    ),
                    style="italic dim",
                )
            table.add_row(
                Text(icon, style=style),
                Text(e.name),
                Text(f"[{e.state}]", style=style),
                Text(str(e.turns), style="dim"),
                status_cell,
            )

        count = len(entries)
        title = f"Subagents — {count} active" if count else "Subagents — idle"
        return Panel(table, title=title, border_style="dim")


async def _run_stream(
    orchestrator: OrchestratorAgent,
    prompt: str,
    view: SystemView,
    *,
    approval_queue: "asyncio.Queue[_bash.ApprovalRequest] | None" = None,
    approval_result_queue: "asyncio.Queue[str] | None" = None,
    approval_mode: asyncio.Event | None = None,
) -> None:
    async def _spin(ui: StreamUI) -> None:
        while True:
            await asyncio.sleep(0.12)
            ui.tick()

    async def _approval_handler(ui: StreamUI) -> None:
        """Drain approval requests, prompt the user, and wait for y/n.

        The Live display is stopped before showing the prompt and restarted
        after receiving the response.  While Live is stopped, tick() calls are
        no-ops and terminal echo of the typed character cannot corrupt Rich's
        cursor-position tracking — which is what caused the repeated panel
        prints when the user typed y/n with Live active.
        """
        if approval_queue is None or approval_result_queue is None or approval_mode is None:
            raise RuntimeError(
                "approval handler requires approval_queue, approval_result_queue, and approval_mode"
            )
        while True:
            req = await approval_queue.get()
            # Set approval_mode BEFORE stopping Live so that if _stdin_reader
            # happens to finish a readline at this exact moment it routes the
            # line to approval_result_queue rather than msg_queue.
            approval_mode.set()

            # Commit an empty renderable and stop Live.
            # Stopped Live: tick() stores renderables but never re-renders,
            # so echo from user input cannot break cursor accounting.
            ui._live.update(Text(""), refresh=True)
            ui._live.stop()

            sys.stdout.write("\a")
            sys.stdout.flush()
            ui._console.rule("[bold yellow]⚠  Approval required[/bold yellow]", style="yellow")
            ui._console.print(Text(f"  $ {req.command[:200]}", style="dim"))
            ui._console.print(Text("  Allow? [y/N]", style="bold yellow"))

            choice = await approval_result_queue.get()

            approval_mode.clear()
            req.approved = choice.strip().lower() in ("y", "yes")
            req.event.set()

            label, sty = ("✓ Allowed", "green") if req.approved else ("✗ Denied", "red")
            ui._console.print(Text(f"  {label}", style=sty))
            ui._console.rule(style="dim")

            # Restart Live — new panels appear below the approval block.
            ui._live.start(refresh=True)

    before = _usage_snapshot(view)
    start = time.perf_counter()
    # A single turn must never kill the REPL: capture any failure from the stream
    # (e.g. an openai.APITimeoutError when the model connection stalls mid-response) and
    # report it after the live display tears down, so the session stays alive and
    # the user can retry. Errors are printed outside the `with` to avoid interleaving
    # with Rich's Live rendering.
    error: Exception | None = None
    with StreamDisplay(view) as ui:
        # Print initial status at the top of the output so the user sees
        # immediate feedback before any tool calls scroll in below.
        # ui._live.console.print(Text("Start Working...", style="yellow dim"))
        tasks: list[asyncio.Task[None]] = [asyncio.create_task(_spin(ui))]
        if approval_queue is not None:
            tasks.append(asyncio.create_task(_approval_handler(ui)))
        try:
            async for chunk in orchestrator.run_streamed(prompt):
                ui.on_chunk(chunk)
            # Stream drained cleanly — settle the indicator on "Completed" (covers
            # turns that end without a finish() tool call, e.g. a plain final text).
            ui._set_message_done()
        except Exception as e:  # one turn's failure must not crash the whole session
            error = e
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    if error is not None:
        _print_turn_error(error)
    _print_usage_summary(view, before, time.perf_counter() - start)


async def _interactive_queue_stream(orchestrator: OrchestratorAgent, view: SystemView) -> None:
    """Interactive loop: persistent orchestrator, queued input, human approval.

    Three concurrent asyncio concerns run in the same event loop:

    1. _stdin_reader — reads lines from stdin in a thread-pool worker.
       Routes each line to either the message queue (normal) or the approval
       result queue (when approval_mode is set).

    2. _run_stream — processes one orchestrator turn at a time.  Internally
       runs the spinner task and the approval handler task.

    3. _approval_handler (inside _run_stream) — drains the approval queue,
       prints the prompt via the Live console, and awaits the user's y/n
       from approval_result_queue.

    The approval_mode Event is the synchronisation point: set() before printing
    the approval prompt, clear() after receiving the response.  _stdin_reader
    checks it after every readline to decide where the line goes.
    """
    loop = asyncio.get_event_loop()

    msg_queue: asyncio.Queue[str | None] = asyncio.Queue()
    approval_queue: asyncio.Queue[_bash.ApprovalRequest] = asyncio.Queue()
    approval_result_queue: asyncio.Queue[str] = asyncio.Queue()
    approval_mode: asyncio.Event = asyncio.Event()

    # Wire the approval channel into the worker_agent module so that shell()
    # closures in any WorkerAgent can post requests here.
    _bash.set_approval_queue(approval_queue)

    def _readline() -> str | None:
        try:
            line = sys.stdin.readline()
            return line.rstrip("\n") if line else None
        except Exception:
            return None

    async def _stdin_reader() -> None:
        while True:
            raw = await loop.run_in_executor(None, _readline)
            if raw is None or raw.lower() == "exit":
                await msg_queue.put(None)
                return
            # Route: if an approval prompt is waiting for y/n, send there;
            # otherwise treat the line as a new user message.
            if approval_mode.is_set():
                await approval_result_queue.put(raw)
            elif raw.strip():
                await msg_queue.put(raw)

    sys.stdout.write("> ")
    sys.stdout.flush()

    reader = asyncio.create_task(_stdin_reader())
    try:
        while True:
            msg = await msg_queue.get()
            if msg is None:
                break
            await _run_stream(
                orchestrator,
                msg,
                view,
                approval_queue=approval_queue,
                approval_result_queue=approval_result_queue,
                approval_mode=approval_mode,
            )
            if msg_queue.empty():
                sys.stdout.write("> ")
                sys.stdout.flush()
    finally:
        _bash.set_approval_queue(None)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


def _usage_snapshot(view: SystemView) -> tuple[int, int, int, int]:
    u = view.usage
    return (u.requests, u.input_tokens, u.output_tokens, u.total_tokens)


def _print_turn_error(error: Exception) -> None:
    """Report a turn that failed mid-stream, without killing the session.

    Network/timeout failures (the common case: the model connection going silent
    past LLM_REQUEST_TIMEOUT) get a targeted hint; anything else is surfaced with
    its type and message so real bugs stay visible."""
    console = Console()
    # openai v3: APITimeoutError subclasses APIConnectionError; TimeoutError is a
    # catch-all for non-SDK timeouts.
    if isinstance(error, (APIConnectionError, TimeoutError)):
        console.print(
            Text(
                f"⚠  Turn interrupted: {type(error).__name__} — the model connection "
                "stalled or dropped mid-response. Your session is intact; retype your "
                "request to retry.",
                style="bold yellow",
            )
        )
        console.print(
            Text(
                f"   If this recurs, raise LLM_REQUEST_TIMEOUT in .env "
                f"(currently {config.LLM_REQUEST_TIMEOUT:.0f}s).",
                style="dim",
            )
        )
    else:
        console.print(Text(f"⚠  Turn failed: {type(error).__name__}: {error}", style="bold red"))
        console.print(Text("   Session preserved — you can continue or retry.", style="dim"))


def _print_usage_summary(
    view: SystemView, before: tuple[int, int, int, int], wall_s: float
) -> None:
    """One-line cost report for the turn just finished: token counts consumed
    since `before` (orchestrator + all subagents) and wall-clock time. Deltas —
    not session totals — so each turn is billed on its own line; the live panel
    shows the running session total."""
    u = view.usage
    d_req = u.requests - before[0]
    d_in = u.input_tokens - before[1]
    d_out = u.output_tokens - before[2]
    d_tot = u.total_tokens - before[3]
    Console().print(
        Text.assemble(
            Text("  usage · ", style="dim"),
            Text(f"{d_tot:,} tokens", style="cyan"),
            Text(f" (in {d_in:,} / out {d_out:,})", style="dim"),
            Text(f" · {d_req} req", style="dim"),
            Text(f" · {format_duration(wall_s)}", style="dim"),
        )
    )


def _new_session(
    session_name: str | None = None, stream_subagents: bool = False
) -> tuple[OrchestratorAgent, SystemView]:
    """Compose one session: a bus, a SystemView fed by it, and an orchestrator
    that publishes to it. Displays render the view; nobody polls the agents.

    `session_name` (from `--session`) names a persistent session whose conversation
    is file-backed and resumes across restarts; without it the session is anonymous
    and in-memory."""
    bus = MessageBus()
    view = SystemView()
    bus.subscribe(view.apply)
    if config.TRACE_ENABLED:
        logger = JsonlLogger.for_run(config.TRACES_DIR)
        bus.subscribe(logger.handle)
        print(f"trace → {logger.path}", file=sys.stderr)
    orchestrator = OrchestratorAgent(
        model=_model, bus=bus, session_id=session_name, stream_subagents=stream_subagents
    )
    if orchestrator.persistent:
        verb = "resuming" if orchestrator.resuming else "new"
        print(
            f"session '{orchestrator.session_id}' ({verb}) → {orchestrator.session_workspace}",
            file=sys.stderr,
        )
    return orchestrator, view


async def main(
    prompt: str | None,
    interactive: bool,
    stream: bool,
    session: str | None = None,
    stream_subagents: bool = False,
) -> None:
    if prompt is not None:
        orchestrator, view = _new_session(session, stream_subagents)
        try:
            if stream:
                await _run_stream(orchestrator, prompt, view)
            else:
                display = LiveDisplay(view)
                display.start()
                before = _usage_snapshot(view)
                start = time.perf_counter()
                try:
                    print(await orchestrator.run(prompt))
                finally:
                    display.stop()
                _print_usage_summary(view, before, time.perf_counter() - start)
        finally:
            await orchestrator.delete_all()
        return

    if interactive:
        orchestrator, view = _new_session(session, stream_subagents)
        try:
            if stream:
                await _interactive_queue_stream(orchestrator, view)
            else:
                loop = asyncio.get_event_loop()
                while True:
                    try:
                        user_input = await loop.run_in_executor(None, input, "> ")
                    except EOFError:
                        return
                    if user_input.lower() == "exit":
                        return
                    if not user_input.strip():
                        continue
                    display = LiveDisplay(view)
                    display.start()
                    before = _usage_snapshot(view)
                    start = time.perf_counter()
                    try:
                        print(await orchestrator.run(user_input))
                    finally:
                        display.stop()
                    _print_usage_summary(view, before, time.perf_counter() - start)
        finally:
            await orchestrator.delete_all()
        return

    print('No prompt provided. Use --prompt "<prompt>" or --interactive to run.')


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="The prompt to send to the orchestrator.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Run in interactive mode.",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        default=False,
        help="Stream output with live tool-call display.",
    )
    parser.add_argument(
        "--stream-subagents",
        action="store_true",
        default=config.STREAM_SUBAGENTS,
        help="Also surface each subagent's own token stream (reasoning + answer) live "
        "in its row. Display-only; the orchestrator still consumes final results. "
        "Defaults to COMET_STREAM_SUBAGENTS.",
    )
    # nargs="?" makes the value optional: bare `--session` falls back to `const`
    # (the conventional name "default"); `--session NAME` uses NAME; omitting the
    # flag leaves it None (anonymous, in-memory).
    parser.add_argument(
        "--session",
        type=str,
        nargs="?",
        const="default",
        default=None,
        metavar="NAME",
        help="Named, resumable session: file-backs the conversation under "
        "workspaces/NAME/ so it survives a restart. Bare --session uses the name "
        "'default'. Omit the flag entirely for a throwaway in-memory session.",
    )
    return parser


def run() -> None:
    """Console-script entry point (`comet`). Also invoked by ``python -m comet.cli``."""
    args = _build_parser().parse_args()
    asyncio.run(
        main(args.prompt, args.interactive, args.stream, args.session, args.stream_subagents)
    )


if __name__ == "__main__":
    run()
