---
name: subagent_use
description: How to plan, schedule, and delegate work to subagents - sequential vs parallel scheduling, allocating skills before spawning, using the shared session workspace, and collecting results. Load this at the start of any task you will complete by delegating to subagents.
---
# Using subagents

You (the coordinator) have **no shell of your own** - you cannot read files, run code,
or install anything directly. Every unit of real work is done by a subagent. Your job
is to decompose the task, spawn the right subagents with the right skills, schedule them,
and stitch their results together.

## Spawn, then invoke - two separate steps

- `spawn_subagent(agent_name, objective, skills=[...])` creates an agent. The **objective
  is its persistent identity** - who it is and what it owns - not the task. Write it as a
  role ("You own data extraction from the source spreadsheet"), not an instruction.
- `invoke_subagent(agent_id, message)` sends the **actual task**, and returns *immediately*
  (non-blocking). The task returns nothing here - you collect it separately.
- Get results with `wait_for_subagent_result(agent_id)` (a specific one) or
  `wait_for_first_subagent_result()` (whichever finishes next).

A subagent keeps its workspace and its conversation across invocations, so you can give it
follow-up tasks and it remembers what it already did.

## Scheduling: sequential vs parallel

Decide from the **data dependencies** between subtasks.

- **Parallel** - independent subtasks with no ordering between them. Spawn several, call
  `invoke_subagent` on each (each returns at once), then collect them as they finish:
  loop `wait_for_first_subagent_result()` until all are in. Use this to fan out
  (e.g. analyze six sheets at once).
- **Sequential** - when B needs A's output, or steps must happen in order. Invoke A, then
  `wait_for_subagent_result(A)`, then invoke B using A's result (or have B read A's file
  from the shared workspace, see below).
- **Mixed (a DAG)** is normal: run independent branches in parallel, then a sequential
  join step that depends on them.

Don't over-fan-out: the backend serves a limited number of concurrent generations, so
dozens of parallel subagents may just queue with no visible progress. Batch if needed.

## Allocate skills *before* spawning

Skills are folded into a subagent's instructions **at spawn time and fixed for its
lifetime - you cannot add them later.** So decide up front:

1. Call `list_skills` and read the descriptions.
2. For each subtask, judge which skills would genuinely help that subagent do it
   (e.g. give the spreadsheet-analysis subagent the `xlsx` skill).
3. Pass exactly those via `spawn_subagent(..., skills=[...])`.

Give each subagent only the skills relevant to *its* job - don't attach everything. If
you're unsure what a skill covers, `load_skill` / `list_skill_resources` to inspect it
first, then spawn.

## Use the shared session workspace

All subagents in a session share **one working directory**. Files written there persist
across turns and are visible to every other subagent. Exploit this for hand-offs:

- A producer subagent writes a file (e.g. `report.md` or `data.json`); a downstream or
  parallel subagent reads it. Tell each subagent the **filename** to read and to produce.
- For a follow-up like "modify the earlier report", either re-invoke the **same** subagent
  (it still has context) or spawn a fresh one and point it at the existing file by name -
  don't assume the directory is empty; instruct subagents to check for existing files.

## Write tasks that get done

- **Objective** = identity/scope (persistent). **Invoke message** = the concrete task now.
- Be explicit about inputs (exact file paths), expected outputs (filename + format), and
  the done-criteria. Ambiguity makes subagents guess.
- Instill persistence: assume setup (installs, downloads) is expected work, not a blocker;
  tell them to figure it out and not stop early.

## Collect, then finish

- A subagent's result flows back into *your* context, so keep each task scoped tightly and
  ask for terse output (paths, metrics, errors - not narration).
- Gather every result you need **before** calling `finish(summary)`, which ends the run.
- `list_subagents` shows who's active and their status; `delete_subagent` frees one you're
  done with.

## Anti-patterns

- Micromanaging one subagent step-by-step when a single clear task would do.
- Fanning out dependent steps in parallel (races, missing inputs) - order them instead.
- Forgetting to attach a skill at spawn, then being unable to add it.
- Assuming a clean workspace when an earlier turn already produced files there.
