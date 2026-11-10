# Comet — Project Restructure Plan

Status: **All phases complete (1–3)** · Last updated: 2026-07-04

> **Phase 3 done** (observability): `message_bus/sinks/jsonl_logger.py` appends every
> event as one JSON line to a per-run trace (`logs/run_<ts>.jsonl`, gitignored).
> `cli._new_session()` subscribes it when `config.TRACE_ENABLED` (env `COMET_TRACE`,
> default on) and prints `trace → <path>` to stderr. The old `SKILLS.txt` audit is
> now a bus-native `SubagentSpawned(skills=…)` line in the trace — verified: a run
> captures OrchestratorStatus/Skills, SubagentSpawned(skills=['xlsx']), SubagentDeleted.
> Restructure complete; future sinks (web UI, metrics) attach without touching agents.

> **Phase 2 done** (message_bus + display inversion): `message_bus/` added
> (`events.py` typed events, `bus.py` pub/sub with exception-isolated sinks);
> `display/view.py` `SystemView` reducer renders from events. Agents now *publish*
> (turn/status/tool/skills/spawn/command/state/delete) and no longer import
> `display`; `LiveDisplay` and the cli `StreamDisplay` render a `SystemView` and no
> longer poll agent internals. The agents↔display import cycle is gone. cli is the
> composition root: `_new_session()` builds bus+view, subscribes `view.apply`, and
> constructs `OrchestratorAgent(model, bus)`. Verified end-to-end: real orchestrator
> methods + simulated worker events update the view; both displays render it.
> Agents keep their own `status/turns/last_command` for the `list_subagents` tool
> (additive — the display just stopped reading them).
> **Next:** Phase 3 — `sinks/jsonl_logger.py` trace + `SubagentSpawned` audit.

> **Phase 1 done** (structure, runnable, no behavior change): `config.py` added;
> `agents/models.py → llm/models.py`; `skills/models.py → skills/registry.py`;
> `tools/` created (`context.py`, `bash.py` extracted from worker, `filesystem.py`
> factories — the latter not yet wired to workers); `main.py → comet/cli.py` +
> `[project.scripts] comet`; `__init__.py` added to every package; repo-root path
> bug fixed (workspaces/ & skills/ no longer resolved inside `comet/`). Also removed
> the orphaned buggy `_record_spawned_skills` (returns in Phase 3 as a bus event)
> and fixed two move typos (`super().__init__`, a stray backtick).
> **Behavior note:** LLM env vars are now read via `config` with defaults, so an
> import no longer hard-crashes when `.env` is absent (it did before).
> **Next:** Phase 2 — message_bus + display inversion.

Goal: consolidate the agent system into a single `comet/` package with clear module
boundaries, and abstract the coordinator↔worker↔UI message flow behind a
**message bus** so display, logging, and future integrations become swappable
subscribers instead of hard-wired callers.

---

## 1. Locked decisions

| Topic | Decision |
|---|---|
| **Display ↔ bus** | `comet/display/` stays a standalone module but only **subscribes** to the bus (push). Agents never import display. |
| **Entry point** | `comet/cli.py`, exposed as a console script `comet = "comet.cli:run"` in `pyproject.toml`. |
| **Skill library location** | SKILL.md content stays at **repo-root `skills/`** (user-editable data). `comet/skills/` is loader **code**, pointed at the library via `config`. |
| **Migration** | **Phased**: P1 structure + tools + config (stays runnable) → P2 message_bus + display inversion → P3 observability sinks. |

---

## 2. Core principle: dependency direction (acyclic)

```
config        <- (leaf, imports nothing internal)
message_bus   <- (leaf: pure events + pub/sub, imports nothing internal)
llm           -> config
skills        -> config
tools         -> message_bus, config            (NOT agents)
agents        -> llm, skills, tools, message_bus, config   (NOT display)
display       -> message_bus, config            (NOT agents)
cli           -> everything (composition root: builds bus, wires sinks, runs)
```

The two rules that kill today's coupling:
1. **Agents publish, never render.** No agent module imports `display`.
2. **Display subscribes, never polls.** No display module imports `agents`/`worker_pool`.

Both meet only at `message_bus` (the event contract) and are wired together in `cli`.

---

## 3. Target layout

```
comet/
  __init__.py
  cli.py                      # entry point (from today's root main.py); console script `comet`
  config.py                   # paths + settings: WORKSPACES_ROOT, SKILLS_DIR, model/env, limits

  llm/
    __init__.py
    models.py                 # ConcurrencyLimitedModel + client construction  (was agents/models.py)

  agents/
    __init__.py               # public API: OrchestratorAgent, WorkerAgent, WorkerAgentPool
    orchestrator.py
    worker.py
    worker_pool.py
    hooks.py                  # OrchestratorHooks / WorkerAgentHooks (split out of orchestrator/worker) [P2]

  skills/
    __init__.py
    registry.py               # SkillsRegistry + Skill/SkillFrontmatter/SkillError  (was skills/models.py)

  tools/
    __init__.py
    context.py                # ToolContext: workspace_dir + emit(bus) handle passed to every tool
    bash.py                   # shell tool factory  (extracted from worker.py closure)
    filesystem.py             # read / write / edit tool factories

  message_bus/
    __init__.py
    events.py                 # typed event dataclasses (the contract)
    bus.py                    # MessageBus: publish() / subscribe(); multiple async subscribers
    sinks/
      __init__.py
      jsonl_logger.py         # durable per-run event trace (observability + audit)

  display/
    __init__.py
    display.py                # LiveDisplay — a bus subscriber holding its own view-model

skills/                       # DATA: SKILL.md library (xlsx, parallel_research, parameter_golf) — unchanged
workspaces/                   # runtime subagent workspaces (gitignored)
pyproject.toml                # + [project.scripts] comet = "comet.cli:run"
RESTRUCTURE_PLAN.md           # this file
```

---

## 4. Module responsibilities

### `config.py`
Single source of truth for what is currently hardcoded in scattered modules
(`_WORKSPACES_ROOT` in `worker.py`, `_SKILLS_DIR` in `orchestrator.py`, model/env
setup). Exposes resolved `Path`s and settings; imports nothing internal.

### `llm/`
The LLM model wrapper (`ConcurrencyLimitedModel`) and client construction. Renamed
out of `agents/models.py` so "model" stops meaning three things.

### `agents/`
Orchestrator, worker, and pool — the behavior. Produces **events** to the bus for
anything the UI/logs care about (status, turns, spawns, tool calls, results).
Builds worker tools from `tools/` factories. Never imports `display`.

### `skills/`
`registry.py` — discovery + progressive-disclosure loading of the root `skills/`
library. (Loader code; the content is data at repo root.)

### `tools/`
Reusable agent tools as **factories** that take a `ToolContext` (workspace dir +
an `emit` handle to the bus). `bash.py` is today's `shell` closure lifted out of
`worker.py`; `filesystem.py` adds read/write/edit. Lets both orchestrator and
workers share the same tools.

### `message_bus/`
The abstraction the whole restructure is for.
- `events.py`: typed dataclasses, each with `ts` + `source` id. Initial set:
  `OrchestratorStatus`, `OrchestratorTurn`, `SkillLoaded`, `SubagentSpawned(id, name, skills)`,
  `SubagentStatus`, `SubagentTurn`, `ShellCommand`, `ToolCallStarted`, `ToolCallFinished`,
  `SubagentResult`. (Extend as needed.)
- `bus.py`: `publish(event)` fans out to all subscribers; `subscribe(handler)`.
  Async-friendly (per-subscriber queue), multiple independent sinks.
- `sinks/jsonl_logger.py`: writes one JSON line per event → a replayable trace.

### `display/`
`LiveDisplay` becomes a **subscriber**: it maintains its own view-model (dict of
agent states) from events and re-renders, instead of polling `pool.list_entries()`
and orchestrator fields. Swappable and testable in isolation.

---

## 5. File migration map

| Today | Becomes |
|---|---|
| `main.py` (root) | `comet/cli.py` (+ console script) |
| `comet/agents/models.py` | `comet/llm/models.py` |
| `comet/skills/models.py` | `comet/skills/registry.py` |
| `comet/agents/worker.py` (shell/set_status closures) | tool logic → `comet/tools/bash.py` (+ `context.py`); worker builds tools from factories |
| `comet/display/display.py` (pull) | `comet/display/display.py` (push, bus subscriber) |
| hardcoded `_WORKSPACES_ROOT`, `_SKILLS_DIR`, model setup | `comet/config.py` |
| *(new)* | `comet/message_bus/{events,bus}.py`, `sinks/jsonl_logger.py` |
| *(new)* | `__init__.py` in every `comet/*` package |

---

## 6. Phased execution

### Phase 1 — structure (always runnable, no behavior change)
1. Add `__init__.py` to all `comet/` packages; expose clean public APIs.
2. Create `comet/config.py`; move paths + model/env there; update references.
3. Rename `skills/models.py → skills/registry.py`; fix imports.
4. Move `agents/models.py → llm/models.py`; fix imports.
5. Create `tools/` (`context.py`, `bash.py`, `filesystem.py`); rewire `worker.py`
   to build tools from factories. Behavior identical.
6. Move `main.py → comet/cli.py`; add `[project.scripts]`. Display bootstrapping
   unchanged for now.
7. **Checkpoint:** full end-to-end run, identical behavior.

### Phase 2 — message bus + display inversion
1. Add `message_bus/events.py` + `bus.py`.
2. Inject the bus into orchestrator + workers; publish events at the points that
   currently mutate `status` / `turns` / `last_command` and at spawn/tool/result.
3. Rewrite `display/display.py` to subscribe and hold its own view-model; delete
   its `comet.agents.*` imports and orchestrator's `import LiveDisplay`. `cli`
   wires display to the bus.
4. (Optional) split `hooks.py` out.
5. **Checkpoint:** display looks identical, now push-based; agent↔display import cycle gone.

### Phase 3 — observability
1. Add `sinks/jsonl_logger.py`, subscribed in `cli` → per-run event trace.
2. Fold the earlier **SKILLS.txt audit** into a bus-native `SubagentSpawned(skills=...)`
   event captured by the jsonl sink (supersedes the standalone file write).
3. Future sinks (web UI, metrics) attach without touching agents.

---

## 7. Carry-over / notes

- **In-flight audit change:** the `_record_spawned_skills` / `SKILLS.txt` edit was made
  to the *old* root `orchestrator.py` (now moved). Do **not** re-apply it as-is — it is
  reframed as a `SubagentSpawned` event + jsonl sink in Phase 3.
- **Motivating bug:** a subagent mishandled the merged-cell `功能活性` sheet (missed a
  species) because the `xlsx` skill was never in its context. Phase 3's event trace makes
  "was this skill loaded?" answerable without inference.
- **Open for later:** whether `set_status` becomes a pure bus event or stays a tool;
  where run-scoped artifacts (traces, workspaces) are rooted; console-script packaging details.
```
