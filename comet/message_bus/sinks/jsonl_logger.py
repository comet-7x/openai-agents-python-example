"""JSONL bus sink — append every event as one JSON line to a per-run trace.

Subscribe `handle` to a `MessageBus` and you get a replayable record of the run:
what the orchestrator did, and — via `SubagentSpawned` — which subagents were
created with which skills. That last point supersedes the old per-workspace
`SKILLS.txt` audit: instead of inferring after the fact whether a subagent that
mishandled a spreadsheet was given the `xlsx` skill, you just grep the trace.

A logging failure must never break the run, so writes are best-effort.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

from comet.message_bus.events import Event


class JsonlLogger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a", encoding="utf-8")

    @classmethod
    def for_run(cls, traces_dir: Path, run_id: str | None = None) -> JsonlLogger:
        run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        return cls(traces_dir / f"run_{run_id}.jsonl")

    def handle(self, event: Event) -> None:
        record: dict[str, object] = {"event": type(event).__name__}
        if is_dataclass(event) and not isinstance(event, type):
            record.update(asdict(event))
        with contextlib.suppress(OSError, ValueError, TypeError):
            self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._file.flush()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._file.close()
