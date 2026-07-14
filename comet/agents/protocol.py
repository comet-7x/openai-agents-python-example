"""Typed contract for orchestrator↔subagent delegation.

A leaf within `agents/`: pure Pydantic types plus small pure helpers, importing
nothing else from `comet`. The orchestrator authors a `SubagentRequest` (as the
structured arguments of `invoke_subagent`) and, at finish time, submits a list of
`CriterionVerdict`. `SubagentResponse` is the worker's structured return channel —
used once the backend is confirmed to honor `output_type` (until then the worker
still returns free text and this type is unused on the wire).

Why this exists: the previous free-text `message`→`final_output` round-trip carried
no *definition of done*, so the orchestrator had nothing to verify against and just
relayed the worker's self-report. `SubagentRequest.acceptance` makes the acceptance
criteria explicit and machine-routable — each `Criterion` is pre-tagged with how it
will be checked: `mechanical` (the orchestrator self-checks the workspace with its
read-only file tools) or `judgment` (an independent verifier subagent decides).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

CheckMode = Literal["mechanical", "judgment"]

# A fenced ```json ... ``` block; the inner text is captured non-greedily so nested
# braces in the manifest don't confuse the match. Workers append one such block as
# their structured report (the backend rejects json_schema output_type, so we parse
# it out of the free-text final message instead — see SubagentResponse.parse).
_FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class Criterion(BaseModel):
    """One acceptance criterion: a checkable statement of what "done" requires,
    tagged at authoring time with how it will be verified."""

    id: str
    describe: str
    check_mode: CheckMode = "mechanical"


class SubagentRequest(BaseModel):
    """The orchestrator's delegation to a subagent: what to do (`task`), what it may
    read (`inputs`), and what "done" means (`acceptance`)."""

    task: str
    inputs: list[str] = Field(default_factory=list)
    acceptance: list[Criterion] = Field(default_factory=list)

    def render(self) -> str:
        """Flatten into the prompt text handed to the worker. The worker sees the
        acceptance criteria so it can aim to satisfy them, not just 'attempt the task'."""
        parts = [self.task.strip()]
        if self.inputs:
            parts.append("## Inputs\n" + "\n".join(f"- {p}" for p in self.inputs))
        if self.acceptance:
            lines = [f"- ({c.check_mode}) {c.describe}" for c in self.acceptance]
            parts.append(
                "## Acceptance criteria (your work is judged against these)\n" + "\n".join(lines)
            )
        return "\n\n".join(parts)


class Deliverable(BaseModel):
    """One concrete artifact a subagent produced — machine-checkable evidence
    (path + size/rows/key fields) to reconcile against the acceptance criteria."""

    path: str
    kind: Literal["file", "metric", "answer"] = "file"
    detail: str = ""


class SubagentResponse(BaseModel):
    """A worker's structured result. The backend rejects json_schema `output_type`,
    so the worker instead appends a fenced ```json block to its free-text final
    message and `parse` recovers this from that text."""

    status: Literal["done", "blocked", "partial"] = "done"
    manifest: list[Deliverable] = Field(default_factory=list)
    summary: str = ""
    blockers: str = ""

    @classmethod
    def parse(cls, text: str) -> SubagentResponse:
        """Recover a response from a worker's free-text final message: validate the
        last fenced ```json block found. If none validates, wrap the whole message as
        the summary (status='done') — a worker that ignored the format loses only the
        structured manifest, it never crashes the orchestrator."""
        for raw in reversed(_FENCED_JSON.findall(text or "")):
            try:
                return cls.model_validate_json(raw.strip())
            except (ValidationError, ValueError):
                continue
        return cls(status="done", summary=(text or "").strip())

    def render(self) -> str:
        """Compact, orchestrator-facing rendering (what wait_for_*_result returns)."""
        lines = [f"status: {self.status}"]
        if self.manifest:
            lines.append("deliverables:")
            for d in self.manifest:
                detail = f" — {d.detail}" if d.detail else ""
                lines.append(f"  - {d.path} ({d.kind}){detail}")
        if self.summary:
            lines.append(f"summary: {self.summary}")
        if self.blockers:
            lines.append(f"blockers: {self.blockers}")
        return "\n".join(lines)


class CriterionVerdict(BaseModel):
    """The orchestrator's per-criterion ruling, gathered before it may finish."""

    criterion_id: str
    verdict: Literal["pass", "fail"]
    evidence: str = ""


def merge_acceptance(base: list[Criterion], extra: list[Criterion]) -> list[Criterion]:
    """Overlay `extra` onto `base`, deduplicating by `id` (later wins). Used to
    combine a skill's declared baseline criteria with the orchestrator's
    task-specific ones — the "两者叠加" rule — deterministically, without relying on
    the model to copy the skill's criteria into its request."""
    by_id: dict[str, Criterion] = {c.id: c for c in base}
    for c in extra:
        by_id[c.id] = c
    return list(by_id.values())
