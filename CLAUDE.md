# CLAUDE.md — comet

An orchestrator + subagent system built on the OpenAI **Agents** SDK. A single
orchestrator LLM plans a task and delegates to local worker subagents, each with a
sandboxed shell, coordinating them to completion. All code lives in the `comet/`
package; `skills/` (data) and `workspaces/`/`logs/` (runtime) sit at the repo root.

## Run it

```bash
# from the repo root — always run as a MODULE, not `python comet/cli.py`
uv run python -m comet.cli --stream --interactive     # REPL + live UI
uv run python -m comet.cli --stream --prompt "..."    # one-shot
uv run python -m comet.cli --prompt "..."             # no live UI, plain output
uv run python -m comet.cli --session mytask --stream --interactive  # resumable session
uv run python -m comet.cli --stream --stream-subagents --interactive  # + live subagent tokens
```

`--session NAME` makes the session **resumable**: the orchestrator conversation is
file-backed (`workspaces/NAME/session.db`) and the workspace dir is stable, so re-running
with the same name picks the conversation back up (it prints `session 'NAME' (resuming)`).
Bare `--session` (no value) uses the conventional name `default`; omitting the flag
entirely gives an anonymous session (timestamped workspace, in-memory conversation,
discarded on exit). Caveat: only the *orchestrator* conversation resumes — subagents are
not reconnected (see "Deferred").

Requires a `.env` with `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` (optional:
`LLM_MAX_CONCURRENCY`, `LLM_REQUEST_TIMEOUT`, `COMET_TRACE=0` to disable traces,
`COMET_TZ` to set the timezone for user-facing timestamps — any IANA name like
`America/New_York`; defaults to `Asia/Shanghai`; `COMET_STREAM_SUBAGENTS=1` as the
default for `--stream-subagents`).

`--stream-subagents` (or `COMET_STREAM_SUBAGENTS=1`) makes each **subagent** run via
`Runner.run_streamed` and publish its own token deltas as `SubagentOutput` events, shown
as a live tail in the subagent's row (reasoning + answer). It is **display-only**: the
orchestrator still consumes each subagent's *final* result, unchanged (see "Subagent
token streaming" below). Off by default — subagents run non-streamed.

> **Timeouts & resilience:** `LLM_REQUEST_TIMEOUT` (default **120s**) is passed as a
> scalar to `AsyncOpenAI`, so httpx applies it to *all* timeout categories — including
> **read**. While streaming, the read timeout bounds the gap *between* chunks, not the
> whole request, so a model that goes silent mid-response (a long reasoning pass, a slow
> `LLM_BASE_URL`) can trip `httpx.ReadTimeout`. Raise it for thinking backends. A turn
> that fails mid-stream — timeout or any other error — is caught per-turn in
> `cli.py::_run_stream`: it reports the error (`_print_turn_error`) and keeps the
> interactive session alive to retry, instead of crashing the REPL.

> **Gotcha — name collision:** our package `comet/agents/` collides with the
> third-party `agents` library. Running `python comet/cli.py` puts `comet/` on
> `sys.path[0]` and shadows the library → `ImportError`. Run as `python -m comet.cli`.
> `cli.py` has a guard that self-corrects direct execution, but `-m` is canonical.

## Test

```bash
uv run pytest          # offline smoke suite in tests/ (no LLM/network); ~29 tests
ruff check comet/ tests/
```

## Architecture

```
comet/
  cli.py            composition root: builds bus + view + trace, wires displays, runs
  config.py         paths (repo-root anchored) + LLM/trace settings; imports nothing internal
  llm/models.py     ConcurrencyLimitedModel (semaphore-gated OpenAIChatCompletionsModel)
  agents/           orchestrator, worker, worker_pool, stream (StreamChunk protocol), protocol (delegation Request/Response + acceptance types)  — PUBLISH events; never import display
  skills/registry.py  progressive-disclosure loader for the skills/ library
  tools/            bash (shell + approval gates + workspace-escape guard), filesystem (workspace-confined read/list/write/edit), context
  message_bus/      events, bus (pub/sub), sinks/jsonl_logger  — leaf; imports nothing internal
  display/          view (SystemView reducer) + display (LiveDisplay)  — render the view; never import agents
```

**Dependency rule (acyclic):** `config` and `message_bus` are leaves. `agents`
depend on `llm/skills/tools/message_bus/config` but **not** `display`. `display`
depends on `message_bus`/`config` but **not** `agents`. `cli` wires everything.

### Message bus (the backbone)

Agents publish typed events (`message_bus/events.py`) — `OrchestratorTurn/Phase/Tool/Status/Skills`, `SubagentSpawned/State/Turn/Status/Command/Deleted`, `UsageReported`.
Consumers subscribe:

- `display/view.py::SystemView.apply` — reduces events into a render model; both
  `LiveDisplay` and the cli `StreamDisplay` render it (push-based, no polling).
- `message_bus/sinks/jsonl_logger.py` — appends every event as a JSON line to
  `logs/run_<timestamp>.jsonl` (a replayable trace; the session prints its path to stderr).

`SubagentState` = machine lifecycle (`idle/running/done/error`); `SubagentStatus` =
the agent's own free-text message. Same split on the orchestrator side.

### Usage accounting (tokens + time)

Each completed run publishes one `UsageReported(source, requests, input/output/total tokens, elapsed_s)` — the orchestrator after `run`/`run_streamed` (from
`result.context_wrapper.usage`), and every subagent from its `_on_done` callback (so
it's counted even if the orchestrator never awaits it). `SystemView.usage` sums them
into session totals; the live panel shows the running total and the CLI prints a
per-turn delta summary (`usage · N tokens (in/out) · R req · Ts`). The JSONL sink
records each event, giving a per-agent cost audit in the run trace.

- **Wall-clock vs. agent-time:** the summary reports wall-clock; `UsageView.agent_seconds`
  is *cumulative* per-run time and can exceed wall-clock because subagents run concurrently.
- **Gaps:** an errored run has no `RunResult`, so it reports time but **zero tokens**;
  a cancelled subagent reports nothing. Numbers are only populated if the backend
  returns token usage — some OpenAI-compatible `LLM_BASE_URL` servers return zeros.
- **Streaming needs `include_usage=True`:** both agents set `ModelSettings(..., include_usage=True)`. The SDK only requests `stream_options={"include_usage": True}`
  by default for the *official* OpenAI client; with a custom `LLM_BASE_URL` it would
  otherwise omit it and stream **zero usage** (`models/chatcmpl_helpers.py`). The
  non-stream path reads `response.usage` directly and is unaffected.

### Streaming CLI display & reasoning

`cli.py` renders the streamed orchestrator run through `StreamUI → RobustStreamUI → StreamDisplay`; the reasoning it shows is only as good as what `orchestrator.run_streamed`
surfaces.

- **Two reasoning channels.** The SDK delivers reasoning on *both* a `content` channel
  (`ResponseReasoningTextDeltaEvent` + `ReasoningItem.raw_item.content`) and a `summary`
  channel (`ResponseReasoningSummaryTextDeltaEvent` + `.summary`). o-series and many
  OpenAI-compatible backends populate only `summary`, so listening to `content` alone
  left reasoning **blank**. `run_streamed` handles both: either delta → a `thinking`
  chunk, and a `ReasoningItem` falls back to `.summary` when `.content` is empty →
  `thinking_done`.
- **Status lifecycle.** The spinner label moves `Client… → Reasoning… → Waiting… → Completed` (`_set_reasoning`/`_set_waiting`/`_set_message_done`). Both `finish()` (the
  `StopAtTools` terminator, whose tool_output *is* the turn's end) and a clean stream
  drain settle on `Completed`; `set_status` reverts to `Waiting…`. Reasoning is committed
  to scrollback once as a dim `◌ Reasoning ·` line, guarded by the `_thinking` flag so a
  late `thinking_done` (raw deltas arrive *before* their `RunItemStreamEvent`) isn't
  printed twice.
- **Elapsed time.** `display.py::format_duration` (rollover `45s → 1m 23s → 1h 05m`)
  formats the settled per-turn usage summary; `format_clock` (fixed-width `00:42 → 1:05:03`) drives the live clock ticking in both the `StreamDisplay` and `LiveDisplay`
  panel titles.

### Skills

`skills/<name>/SKILL.md` (frontmatter + body) + optional `scripts/`, `references/`,
`assets/`. **Progressive disclosure** (`registry.py`): Level 1 (METADATA — `name` +
`description`) is parsed for every skill at startup and **inlined into the orchestrator
system prompt** by `_build_instructions` (rebuilt per turn), so the orchestrator always
knows which skills exist without a discovery tool-call; `list_skills` just re-lists the
same set. Level 2 (INSTRUCTIONS — full body via `load_skill`) and Level 3 (RESOURCES —
`scripts/`/`references/`/`assets/` names via `list_skill_resources`/`read_skill_reference`)
load lazily, on demand. A skill's instructions are folded into a subagent's objective at
spawn time (`spawn_subagent(..., skills=[...])`).
Notable: `skills/xlsx/` — `read_merged.py` (merged-cell forward-fill) and
`normalize.py --auto` (unpivots cross-tab sheets to tidy long records,
vocabulary-free structure detection). `skills/course-knowledge-extraction/` —
turns a syllabus into a knowledge map + deep-dive + machine-readable knowledge base;
its `scripts/md_to_docx.{py,js}` are narrow md→`.docx` converters that optionally also
emit `.pdf` + PNG page-previews (`--pdf`/`--preview N`, via `soffice`/`pdftoppm`, each
conversion isolated to a throwaway LibreOffice profile so parallel subagents don't
collide on the profile lock).

### Workspaces & sessions

One shared workspace per session: `workspaces/sess_<timestamp>/`, used as the cwd
for **every** subagent in that session, so a deliverable one writes (e.g. `report.md`)
is visible to siblings and to a later subagent asked to modify it. By default sessions are
in-memory (`SQLiteSession(":memory:")`), so context does **not** survive a process
restart — files persist on disk but nothing auto-reconnects. Passing `--session NAME`
file-backs the *orchestrator* conversation (`workspaces/NAME/session.db`) so it resumes;
subagents still don't reconnect (see "Deferred").

Each worker's toolset is `set_status`, `shell`, and `read_file`/`write_file`/`edit_file`
(`tools/filesystem.py`), plus any tools a loaded skill adds — all sharing one
`ToolContext` bound to the session workspace. The prompt steers workers to prefer the
file tools over shell heredocs/`echo >` for workspace files (no quoting pitfalls).

The two families confine differently. The **shell softly** confines *writes*:
`bash.py::check_workspace_escape` (Gate 1b) blocks `cd`/redirect/`rm` whose target
resolves outside the workspace, while deliberately leaving reads (`cat /etc/hosts`) and
package installs / `git clone` (which write to system dirs) alone — regex-based, so it
catches accidents and casual escapes, not a determined adversary. The **file tools are
hard-confined** (`filesystem.py::_resolve`): reads *and* writes must resolve inside the
workspace, so `read_file('/etc/hostname')` is rejected where `cat` would succeed. The
orchestrator prompt names the workspace path and tells subagents to keep files inside it
with distinct filenames.

### Delegation contract & verification (agents/protocol.py)

Delegation is a typed contract, not a free-text hand-off. `agents/protocol.py` is a
leaf within `agents/` (pure Pydantic, imports nothing else internal):

- **`SubagentRequest`** = `task` + `inputs` + **`acceptance`** (a list of `Criterion`,
  each a checkable statement tagged `check_mode: mechanical | judgment`). The
  orchestrator authors it as the structured arguments of `invoke_subagent`; `.render()`
  flattens it into the worker prompt so the worker *sees the definition-of-done*, not
  just "attempt the task". This exists because the old `message`→`final_output` round-trip
  carried no acceptance criteria, so the orchestrator had nothing to verify against and
  just relayed the worker's self-report.
- **Acceptance is skill ⊕ orchestrator ("两者叠加")**, merged *deterministically* in
  `invoke_subagent` (not left to the model to copy): a skill declares baseline criteria
  in its `SKILL.md` frontmatter (`acceptance:` → `registry.load_acceptance`, returned as
  raw dicts so `skills/` never imports the `agents` layer), and `merge_acceptance` overlays
  the orchestrator's task-specific ones (later wins by `id`).
- **`SubagentResponse`** = `status` + `manifest` (deliverables with machine-checkable
  `detail`) + `summary` + `blockers`. The backend rejects json_schema `output_type` (see
  gotcha below), so the worker instead appends a fenced ```json block to its final message
  and `SubagentResponse.parse` recovers it (degrading to summary-only if a worker ignores
  the format — never crashing). `wait_for_subagent_result` returns the parsed `.render()`.

**Verification loop before finish (a hard gate, not a prompt hope).** A subagent's report
is a *claim*; the orchestrator's "done" must rest on evidence it gathered itself:

- The orchestrator has its **own workspace tools** (`read_file`, `list_dir`, `write_file`,
  `edit_file` via `filesystem.py`, workspace-hard-confined, plus a workspace-confined
  `shell` — see "Orchestrator does the spine" below) so it can check *mechanical* criteria
  against ground truth directly, without spawning a subagent just to `stat` a file. *Judgment*
  criteria go to a fresh **independent verifier subagent** fed only the criteria + artifact
  paths (never the producer's summary, which would anchor it) — this holds even for artifacts
  the orchestrator produced itself; it never self-judges a judgment criterion.
- **`finish` is gated on `verify`.** `verify(verdicts)` sets `self._verified` only when
  every criterion passes (reset at the start of each run). `tool_use_behavior` is a custom
  callable (`_finish_gate`) — not `StopAtTools` — that lets `finish` terminate the run
  *only while `_verified` is true*; an unverified `finish` is refused (finish() returns a
  corrective message) and the run continues. So the model structurally cannot finish on
  unverified work. (A plain no-tool-call final message still ends the run, as before.)

### Orchestrator does the spine (inline execution, not a pure coordinator)

The orchestrator is **not** limited to coordinating — it has its own workspace-confined
execution tools (`write_file`, `edit_file`, and `shell`, sharing `self._fs_ctx` with its
read tools; `orchestrator.py`). The default is **do the work yourself inline**; delegation
is the exception, taken only when a sub-part has a real win. The prompt encodes the rule
("Do it yourself vs. delegate"): `spawn_subagent` only for (1) **parallelism** — genuinely
concurrent independent sub-parts; (2) **isolation** — a sub-part whose verbose intermediate
output you don't want in your context, only its small result; or (3) **skill** — a sub-part
needing a skill's full body loaded inline. Otherwise (small, sequential, context-overlapping)
the orchestrator does it itself. In particular it owns the sequential **spine**: outline,
narrative/structure, and the final **reduce** (assembling subagents' asset outputs into the
finished deliverable). This exists because delegating the spine pays a cold start (re-prime
the whole subagent prompt + a lossy summary hand-off) every time and over-decomposes work one
warm context could hold — the architecture is a *map-reduce over LLM* whose reduce belongs in
the orchestrator, not a thin router that fans everything out.

Confinement is identical to the workers': the file tools hard-confine reads *and* writes
(`filesystem.py::_resolve`), and the `shell` soft-confines writes (`bash.py::check_workspace_escape`)
while allowing reads / installs. The shell's `on_command` publishes each command as an
`OrchestratorStatus` (`$ <cmd>`) so it surfaces in the live display; the command also already
appears as a streamed `tool_call` chunk (`tool_name="shell"`). All three tools are `async def`
(subprocess / `to_thread`-wrapped disk I/O), so they don't block the loop — consistent with
the sync-vs-async dispatch rule below. Cost of this choice: the clean "orchestrator plans /
worker executes" layering blurs, and the orchestrator's growing context absorbs some tool-output
bloat that pure delegation would have kept out.

### Subagent token streaming (opt-in, display-only)

By default subagents run **non-streamed** (`Runner.run`) and surface only lifecycle
events (`SubagentState/Status/Command`). `--stream-subagents` (or `COMET_STREAM_SUBAGENTS=1`)
flips `WorkerAgent._execute` to `Runner.run_streamed`: it drains `stream_events()` inside
the worker's background task and publishes each reasoning/visible delta as a
**`SubagentOutput(id, delta, channel)`** event. `SystemView` reduces these into a
per-subagent `output_tail` (a rolling ~200-char tail, cleared on each new turn and when the
run settles), which both the `LiveDisplay` and CLI `StreamDisplay` show live in the
subagent's row. The flag flows `cli → OrchestratorAgent → WorkerAgentPool → WorkerAgent.stream_output`.

Two invariants make this safe and correct:

- **Parent consumption is unchanged** — this is *only* a display signal. The orchestrator
  still `await`s the worker's `run_task` and reads the **final** result (an LLM tool-result
  must be a complete value, so the "parent boundary is non-streaming" rule holds).
  `_execute` returns the `RunResultStreaming`, whose `.final_output` / `.context_wrapper.usage`
  are read exactly as before once the stream drains.
- **Streaming a subagent never stalls the orchestrator's own stream**, because the worker is
  a background task on the shared loop and every step is non-blocking; publishing deltas is
  cheap. (Same single-event-loop discipline as the sync/async footgun below.)

`SubagentOutput` is **excluded from the JSONL sink** (`jsonl_logger.handle`) — a per-token
signal would bloat the audit trace and flush on every token; the lifecycle events remain the
record.

### Tool dispatch (sync vs async) — a footgun

`function_tool` dispatches on whether the wrapped function is a coroutine
(`agents/tool.py`, the `is_sync_function_tool` branch):

- **`async def` tool → `await the_func(...)`** — run **inline on the event loop**.
- **plain `def` tool → `await asyncio.to_thread(the_func, ...)`** — **offloaded to a
  worker thread**.

So `async` does *not* mean non-blocking — it's the opposite for a blocking body. An
`async def` tool that makes a blocking call (e.g. `Path.read_text()`) stalls the whole
loop, and with it **every concurrently-running subagent**, for the duration. A plain
`def` tool doing the same is safe: the SDK runs it in a thread. The rule, by body:

- **Needs to `await`** (subprocess, network, other coroutines) → `async def`, await
  inside. `shell` (asyncio subprocess) and the orchestrator's `spawn/invoke/wait_*`
  tools are correctly async.
- **Blocking I/O, no async API** (file read/write) → either leave it `def` (the SDK
  thread-offloads it) *or* keep `async def` and wrap the blocking call in
  `await asyncio.to_thread(...)`. The worker file tools (`filesystem.py`) take the
  latter route so the toolset stays uniformly async alongside `shell`, without the
  size-proportional read/write blocking the loop.
- **Trivial in-memory work** → `def` is fine (thread-offload is harmless). The
  orchestrator's `set_status`/`list_skills`/`list_subagents`/`list_skill_resources`/
  `read_skill_reference` are sync on purpose — the last two touch disk but, *being
  sync*, are already thread-offloaded; making them `async def` without `to_thread`
  would re-introduce the footgun.

### Worker `tool_choice` — a custom-backend footgun

Workers **must** use `ModelSettings(tool_choice="auto")`, not `"required"` — the same
setting the orchestrator uses. `"required"` looks safer (it should *force* every turn to
call a tool) but on a custom `LLM_BASE_URL` it does the opposite, for two compounding
reasons:

- **The backend doesn't honor `required`.** The official OpenAI API guarantees
  `tool_choice="required"` returns a valid tool call; many OpenAI-compatible / reasoning
  backends don't. Ours returns an **empty-content message with no `tool_calls`** (the
  output tokens are reasoning tokens). The SDK sees no tool call, treats it as the final
  turn (default `tool_use_behavior`), and ends the run with an **empty `final_output`** —
  the same class of "custom backend ≠ official OpenAI" gotcha as `include_usage` (Usage
  accounting) and the reasoning `summary` channel (Streaming).
- **The SDK's auto-downgrade is gated on prior tool use, so `required` gets stuck.**
  `run_internal/tool_execution.py::maybe_reset_tool_choice` only downgrades
  `required`→`None`/auto once `tool_use_tracker.has_used_tools(agent)` is true — the design
  assumes turn 1 forces a tool, then relaxes so later turns can emit a final message. But
  if the very first forced turn yields *no* tool call (per the bullet above),
  `has_used_tools` never flips, so **every** request keeps `required`. Chicken-and-egg: it
  needs one successful tool call to unlock the downgrade, but `required` is exactly what
  prevents that call. Result: 100% failure, every invoke, every fresh worker — a subagent
  that reaches `[done]` in one request per turn with empty output and **zero**
  `SubagentCommand`/`SubagentStatus` events is the tell.

`auto` sidesteps both: the request never carries `required`, so the worker behaves like the
orchestrator (which is `auto` and always worked on this backend). The strong "relentless
executor" worker prompt keeps it calling tools without the force.

### Structured output (`output_type`) — not available on this backend

Do **not** rely on Agent `output_type=` (json_schema structured output) with the current
`LLM_BASE_URL`. DeepSeek (`deepseek-v4-flash`) rejects it outright:
`400 "This response_format type is unavailable now"` — both alone and combined with a tool
(probed directly; the SDK maps `output_type` to `response_format={"type":"json_schema",…}`).
This is the third member of the "custom backend ≠ official OpenAI" family, alongside
`tool_choice="required"` (above) and `include_usage` / reasoning-`summary` (earlier).

So the worker→orchestrator structured **`SubagentResponse`** is carried the backend-agnostic
way instead: the worker appends a fenced ```json block to its final message and
`protocol.py::SubagentResponse.parse` recovers it (summary-only fallback if absent). If you
later switch to a backend that honors json_schema, the type already exists — just add
`output_type=SubagentResponse` to the worker Agent and drop the parse step. Re-run
`scratchpad/probe_output_type.py`-style probes A (alone) and B (with a tool) before betting
on it; a backend can accept json-mode but still reject json_schema.

## Conventions

- Python ≥3.10 (targets 3.12); `ruff` (line length 100) is the linter/formatter.
- Run everything through `uv run`.
- Tests are offline: construct agents/tools/bus, never issue an LLM request.

## Status & history

Restructure into the `comet/` package is complete (Phases 1–3) plus the shared
session workspace. Full detail and rationale in **`RESTRUCTURE_PLAN.md`**.

**Deferred / known gaps:**

- Resumable sessions: the **orchestrator** conversation now resumes via `--session NAME`
  (file-backed `SQLiteSession` at `workspaces/NAME/session.db`). Still deferred: subagent
  reconnection — a resumed orchestrator's history references subagent ids whose live
  `WorkerAgent` objects no longer exist, so `invoke_subagent(old_id)` fails until the pool
  learns to lazily re-create a worker (with its own file-backed session) for a persisted id.
- ~~`LLM_MAX_CONCURRENCY` is read but not passed to `ConcurrencyLimitedModel`~~ —
  **fixed**: `cli.py` now passes it to the shared model's semaphore, which gates the
  *combined* orchestrator + subagent LLM calls (FIFO-fair, so no worker is starved).
  Set it to what your `LLM_BASE_URL` backend can actually serve — too high floods a
  limited backend and the overflow requests stall with no visible progress (a subagent
  stuck `[running]` at turn 1 is the tell); the default is 4.
- Parallel subagents share one workspace dir — filename collisions are possible;
  the subagent prompt nudges toward distinct scratch filenames.
- Finish-gate cosmetic glitch: if the orchestrator calls `finish` *before* verifying,
  the CLI renders the "Answer" + summary from the tool-call chunk before the gate
  refuses it, so a brief false "Answer" flashes and the run then continues. Correctness
  is unaffected (the gate keeps the run alive); only the streamed display is momentarily
  misleading. Fixing it means teaching `cli.py` to defer the "Answer" render until the
  gate actually terminates — deferred as low-value (the happy path verifies first).
