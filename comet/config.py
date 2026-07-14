"""Central configuration: filesystem paths and runtime settings.

Leaf module — it imports nothing from `comet`, so any module may depend on it
without risking an import cycle.

Paths are anchored to the **repository root** (the parent of the `comet/`
package), not to each module's own location. This matters because the package
move left `Path(__file__).parent` pointing *inside* `comet/` in several modules,
which silently relocated `workspaces/` and `skills/`. Resolving everything from
one place here fixes that class of bug.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import find_dotenv, load_dotenv

# Load .env once, here, so every importer sees a populated environment
# regardless of import order.
load_dotenv(find_dotenv())

# comet/config.py -> comet/ -> repo root
_PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = _PACKAGE_ROOT.parent

# The SKILL.md content library and subagent runtime workspaces both live at the
# repo root as data — not inside the package.
SKILLS_DIR = REPO_ROOT / "skills"
WORKSPACES_ROOT = REPO_ROOT / "workspaces"

# Per-run event traces written by the JSONL bus sink (observability/audit).
TRACES_DIR = REPO_ROOT / "logs"
TRACE_ENABLED = os.environ.get("COMET_TRACE", "1") != "0"

# Timezone for user-facing timestamps — the system-prompt "Today is:" line and
# session-id stamps. Set COMET_TZ to any IANA name (e.g. "America/New_York",
# "UTC"); otherwise defaults to Asia/Shanghai. An unknown name falls back to the
# default with a warning rather than crashing the whole app at import.
_TZ_NAME = os.environ.get("COMET_TZ", "Asia/Shanghai")
try:
    TIMEZONE = ZoneInfo(_TZ_NAME)
except (ZoneInfoNotFoundError, ValueError):
    print(
        f"[comet.config] unknown COMET_TZ={_TZ_NAME!r}; falling back to Asia/Shanghai",
        file=sys.stderr,
    )
    TIMEZONE = ZoneInfo("Asia/Shanghai")

# ── LLM backend settings ─────────────────────────────────────────────────────
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

# Local/shared inference backends often can't serve unlimited concurrent
# generations. Spawning many subagents fires their LLM calls in parallel; past
# this cap, requests just queue at the backend with zero visible progress
# (indistinguishable from a hang). Tune per backend.
LLM_MAX_CONCURRENCY = int(os.environ.get("LLM_MAX_CONCURRENCY", "4"))
LLM_REQUEST_TIMEOUT = float(os.environ.get("LLM_REQUEST_TIMEOUT", "120"))

# Surface subagents' own token stream (reasoning + answer) as SubagentOutput events
# for the live display. Off by default (subagents run non-streamed); the `--stream-subagents`
# CLI flag or COMET_STREAM_SUBAGENTS=1 turns it on. Display-only — it never changes what
# the orchestrator consumes from a subagent (always the final result).
STREAM_SUBAGENTS = os.environ.get("COMET_STREAM_SUBAGENTS", "0") != "0"
