import argparse
import asyncio
import os
import sys

# Running this file directly (`python comet/cli.py`) puts the comet/ directory on
# sys.path[0], which shadows the third-party `agents` package with our own
# comet/agents/ — so `import agents` fails. Swap in the repo root so both resolve.
# No-op under `python -m comet.cli` or the installed `comet` console script.
if os.path.basename(sys.path[0] or "") == "comet":
    sys.path[0] = os.path.dirname(sys.path[0])

from agents import set_tracing_disabled
from openai import AsyncOpenAI
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import comet.tools.bash as _bash
from comet import config
from comet.agents.orchestrator import OrchestratorAgent
from comet.agents.stream import StreamChunk
from comet.display.display import LiveDisplay
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

_model = ConcurrencyLimitedModel(model=config.LLM_MODEL, openai_client=_client)

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
    _transient = True  # subclasses set False to keep the Live area visible on stop

    def __init__(self) -> None:
        self._console = Console(highlight=False)
        self._frame = 0
        self._status_msg = "Thinking…"
        self._pending: dict[str, str] = {}  # call_id → tool_name
        self._last_char = "\n"
        self._text_buf = ""  # partial line shown inside the Live area
        self._thinking = False  # True while receiving "thinking" deltas
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
            self._write_text(chunk.content)

        elif chunk.type == "thinking":
            # Reasoning token delta — show "Reasoning…" in the spinner while
            # it streams. Actual content is surfaced via thinking_done.
            if not self._thinking:
                self._thinking = True
                self._status_msg = "Reasoning…"
                self._live.update(self._make_renderable(), refresh=True)

        elif chunk.type == "thinking_done":
            # Full reasoning block complete — show a dim one-line preview.
            self._thinking = False
            preview = chunk.content.strip().replace("\n", " ")
            if len(preview) > self._VAL_WIDTH:
                preview = preview[: self._VAL_WIDTH - 1] + "…"
            self._live.console.print(f"  [dim italic]◌ {preview}[/dim italic]")
            self._last_char = "\n"
            self._set_thinking()

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

    def _set_thinking(self) -> None:
        self._status_msg = "Thinking…"
        self._live.update(self._make_renderable(), refresh=True)

    def _set_running(self, tool: str) -> None:
        self._status_msg = f"Running {tool}…"
        self._live.update(self._make_renderable(), refresh=True)

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
            # finish() ends the run via StopAtTools — the model never gets a
            # follow-up turn to emit real "text", so its summary IS the final
            # answer. Show it in full, not truncated like a regular tool arg.
            self._live.console.print(Text("Answer", style="bold green"))
            summary = args.get("summary")
            if isinstance(summary, str) and summary.strip():
                self._live.console.print(summary.strip())
                self._last_char = "\n"
            self._set_running(tool)
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

        self._set_running(tool)

    def _render_tool_output(self, chunk: StreamChunk) -> None:
        call_id = chunk.tool_call_id
        tool_name = self._pending.pop(call_id, None) if call_id else None

        if tool_name in ("set_status", "finish"):
            self._set_thinking()
            return

        content = chunk.content.strip().replace("\n", " ")
        if len(content) > self._VAL_WIDTH:
            content = content[: self._VAL_WIDTH - 3] + "…"

        self._live.console.print(f"  [dim]{self._ARROW_IN} {content}[/dim]")
        self._last_char = "\n"
        self._set_thinking()


class RobustStreamUI(StreamUI):
    """StreamUI subclass with prev_type transition tracking.

    Mirrors the logic from print_stream() to handle common model quirks:

    1. text   — skips leading whitespace tokens before real content starts
                (without updating prev_type, same as the `continue` in print_stream);
                prints an "Answer" header on the first real text token.
    2. thinking
              — shows a header only on the first reasoning token
                (guards with prev_type != "thinking").
    3. thinking_done
              — suppressed when it arrives late, i.e. after text has already
                started (RunItemStreamEvent fires after raw deltas finish).
    4. message_done
              — only flushed/acknowledged when content is non-empty, avoiding
                spurious blank output from empty MessageOutputItems.
    5. tool_call / tool_output
              — resets _text_started so an "Answer" header can be re-emitted
                if the model outputs text again after tool results.
    """

    def __init__(self) -> None:
        super().__init__()
        self._prev_type: str | None = None
        self._text_started: bool = False
        self._answer_shown: bool = False  # print "Answer" header only once per run

    def on_chunk(self, chunk: StreamChunk) -> None:
        t = chunk.type

        if t == "thinking":
            if self._prev_type != "thinking":
                self._flush_text()
                self._live.console.print("[dim italic]◌ Reasoning[/dim italic]")
                self._last_char = "\n"
                self._thinking = True
                self._status_msg = "Reasoning…"
                self._live.update(self._make_renderable(), refresh=True)
            # Thinking tokens are not streamed inline (can be very verbose);
            # the thinking_done block will surface a one-line summary.

        elif t == "thinking_done":
            # Late event: thinking_done arrives after text has already started —
            # reset the thinking flag silently and do NOT update _prev_type.
            if self._prev_type != "thinking":
                self._thinking = False
                return
            self._flush_text()
            if chunk.content.strip():
                preview = chunk.content.strip().replace("\n", " ")
                if len(preview) > self._VAL_WIDTH:
                    preview = preview[: self._VAL_WIDTH - 1] + "…"
                self._live.console.print(f"  [dim]  {preview}[/dim]")
                self._last_char = "\n"
            self._thinking = False
            self._set_thinking()

        elif t == "text":
            # Leading whitespace before any real content — discard and do NOT
            # update _prev_type (mirrors the `continue` in print_stream).
            if not self._text_started and not chunk.content.strip():
                return
            if not self._text_started:
                self._flush_text()
                # Transition from thinking → text: reset spinner to "Thinking…"
                if self._thinking:
                    self._thinking = False
                    self._set_thinking()
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

        self._prev_type = t


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

        # Panel title carries the animated spinner + current status message,
        # making it the prominent live indicator at the top of the Live area.
        title = Text.assemble(
            Text(f"{frame} ", style="yellow dim"),
            Text(self._status_msg, style="yellow dim"),
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
            table.add_row(
                Text(icon, style=style),
                Text(e.name),
                Text(f"[{e.state}]", style=style),
                Text(str(e.turns), style="dim"),
                Text(
                    "  ".join(
                        filter(
                            None,
                            [
                                e.status,
                                f"$ {e.last_command}" if e.last_command else None,
                            ],
                        )
                    ),
                    style="italic dim",
                ),
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

    with StreamDisplay(view) as ui:
        # Print initial status at the top of the output so the user sees
        # immediate feedback before any tool calls scroll in below.
        ui._live.console.print(Text("⣾ Thinking…", style="yellow dim"))
        tasks: list[asyncio.Task[None]] = [asyncio.create_task(_spin(ui))]
        if approval_queue is not None:
            tasks.append(asyncio.create_task(_approval_handler(ui)))
        try:
            async for chunk in orchestrator.run_streamed(prompt):
                ui.on_chunk(chunk)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


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


def _new_session() -> tuple[OrchestratorAgent, SystemView]:
    """Compose one session: a bus, a SystemView fed by it, and an orchestrator
    that publishes to it. Displays render the view; nobody polls the agents."""
    bus = MessageBus()
    view = SystemView()
    bus.subscribe(view.apply)
    if config.TRACE_ENABLED:
        logger = JsonlLogger.for_run(config.TRACES_DIR)
        bus.subscribe(logger.handle)
        print(f"trace → {logger.path}", file=sys.stderr)
    orchestrator = OrchestratorAgent(model=_model, bus=bus)
    return orchestrator, view


async def main(prompt: str | None, interactive: bool, stream: bool) -> None:
    if prompt is not None:
        orchestrator, view = _new_session()
        try:
            if stream:
                await _run_stream(orchestrator, prompt, view)
            else:
                display = LiveDisplay(view)
                display.start()
                try:
                    print(await orchestrator.run(prompt))
                finally:
                    display.stop()
        finally:
            await orchestrator.delete_all()
        return

    if interactive:
        orchestrator, view = _new_session()
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
                    try:
                        print(await orchestrator.run(user_input))
                    finally:
                        display.stop()
        finally:
            await orchestrator.delete_all()
        return

    print('No prompt provided. Use --prompt "<prompt>" or --interactive to run.')


def run() -> None:
    """Console-script entry point (`comet`). Also invoked by ``python -m comet.cli``."""
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
    args = parser.parse_args()
    asyncio.run(main(args.prompt, args.interactive, args.stream))


if __name__ == "__main__":
    run()
