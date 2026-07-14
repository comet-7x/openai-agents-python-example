"""Skills subsystem with progressive disclosure.

Layout on disk:

    skills/<name>/SKILL.md          # required — YAML frontmatter + instructions body
    skills/<name>/scripts/          # optional — run via a worker agent's shell tool
    skills/<name>/references/       # optional — read on demand via read_skill_reference
    skills/<name>/assets/           # optional — non-text files (templates, images, ...)

Discovery parses only the frontmatter of every SKILL.md at startup (level
METADATA). The instructions body and resource listings are read lazily, on
first request, and then cached on the Skill object (levels INSTRUCTIONS and
RESOURCES). This keeps a large skill library cheap to enumerate while still
letting an agent pull in full detail only for the skills it actually uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from agents import Tool

DisclosureLevel = Literal[1, 2, 3]  # Progressive disclosure levels for skill loading.
METADATA: DisclosureLevel = 1  # only frontmatter (name, description) is loaded
INSTRUCTIONS: DisclosureLevel = 2  # full SKILL.md body is loaded
RESOURCES: DisclosureLevel = 3  # scripts/references/assets file names are cataloged

_RESOURCE_DIRS = ("scripts", "references", "assets")
_FRONTMATTER_DELIM = "---"


class SkillError(Exception):
    """Raised for malformed skills or invalid resource access."""


@dataclass(frozen=True)
class SkillFrontmatter:
    name: str
    description: str
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] | None = None
    # Baseline acceptance criteria the skill declares as its definition-of-done —
    # raw dicts (id/describe/check_mode), kept vocabulary-free here so this leaf
    # never imports the agents-layer Criterion type. The orchestrator merges these
    # with its own task-specific criteria when it delegates (see agents/protocol.py).
    acceptance: list[dict] = field(default_factory=list)


@dataclass
class Skill:
    frontmatter: SkillFrontmatter
    path: Path
    disclosure_level: DisclosureLevel = METADATA
    instructions: str | None = None
    resource_files: dict[str, list[str]] | None = None

    @property
    def name(self) -> str:
        return self.frontmatter.name

    @property
    def description(self) -> str:
        return self.frontmatter.description

    @property
    def scripts_dir(self) -> Path:
        return self.path / "scripts"

    @property
    def references_dir(self) -> Path:
        return self.path / "references"

    @property
    def assets_dir(self) -> Path:
        return self.path / "assets"


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith(_FRONTMATTER_DELIM):
        raise SkillError("missing YAML frontmatter (file must start with '---')")
    parts = text.split(_FRONTMATTER_DELIM, 2)
    if len(parts) != 3:
        raise SkillError("unterminated YAML frontmatter block")
    return parts[1], parts[2].strip()


def _parse_frontmatter(raw_yaml: str, source: Path) -> SkillFrontmatter:
    data = yaml.safe_load(raw_yaml) or {}
    try:
        name = data["name"]
        description = data["description"]
    except KeyError as e:
        raise SkillError(f"{source}: frontmatter missing required key {e}") from e
    acceptance = data.get("acceptance") or []
    if not isinstance(acceptance, list) or not all(
        isinstance(c, dict) and c.get("id") and c.get("describe") for c in acceptance
    ):
        raise SkillError(f"{source}: 'acceptance' must be a list of {{id, describe}} entries")
    return SkillFrontmatter(
        name=name,
        description=description,
        metadata=data.get("metadata") or {},
        allowed_tools=data.get("allowed-tools"),
        acceptance=acceptance,
    )


class SkillsRegistry:
    """Discovers and lazily loads skills from a directory of skills/<name>/SKILL.md files."""

    def __init__(self, skills_dir: Path) -> None:
        self._skills_dir = skills_dir
        self._skills: dict[str, Skill] = {}
        self._discover()

    def _discover(self) -> None:
        if not self._skills_dir.is_dir():
            return
        for skill_md in sorted(self._skills_dir.glob("*/SKILL.md")):
            try:
                frontmatter_text, _ = _split_frontmatter(skill_md.read_text())
                frontmatter = _parse_frontmatter(frontmatter_text, skill_md)
            except SkillError:
                continue
            self._skills[frontmatter.name] = Skill(frontmatter=frontmatter, path=skill_md.parent)

    def list_skills(self) -> list[Skill]:
        """Level METADATA — cheap; safe to enumerate in full at session start."""
        return list(self._skills.values())

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def load_instructions(self, name: str) -> str:
        """Level INSTRUCTIONS — lazily read and cache the full SKILL.md body."""
        skill = self._skills.get(name)
        if skill is None:
            raise SkillError(f"skill '{name}' not found")
        if skill.instructions is None:
            _, body = _split_frontmatter((skill.path / "SKILL.md").read_text())
            skill.instructions = body
            if skill.disclosure_level < INSTRUCTIONS:
                skill.disclosure_level = INSTRUCTIONS
        return skill.instructions

    def list_resources(self, name: str) -> dict[str, list[str]]:
        """Level RESOURCES — lazily catalog scripts/references/assets file names."""
        skill = self._skills.get(name)
        if skill is None:
            raise SkillError(f"skill '{name}' not found")
        if skill.resource_files is None:
            skill.resource_files = {
                d: sorted(p.name for p in (skill.path / d).iterdir() if p.is_file())
                if (skill.path / d).is_dir()
                else []
                for d in _RESOURCE_DIRS
            }
            skill.disclosure_level = RESOURCES
        return skill.resource_files

    def read_reference(self, name: str, rel_path: str) -> str:
        """Read a file out of a skill's references/ directory."""
        skill = self._skills.get(name)
        if skill is None:
            raise SkillError(f"skill '{name}' not found")
        refs_root = skill.references_dir.resolve()
        target = (skill.references_dir / rel_path).resolve()
        if refs_root not in target.parents and target != refs_root:
            raise SkillError("reference path escapes the skill's references/ directory")
        if not target.is_file():
            raise SkillError(f"reference '{rel_path}' not found in skill '{name}'")
        return target.read_text()

    def load_acceptance(self, name: str) -> list[dict]:
        """A skill's declared baseline acceptance criteria (raw {id, describe,
        check_mode} dicts), or [] if it declares none. The orchestrator converts
        these to Criterion objects and merges them into every delegation that
        equips this skill."""
        skill = self._skills.get(name)
        if skill is None:
            raise SkillError(f"skill '{name}' not found")
        return list(skill.frontmatter.acceptance)

    def load_tools(self, name: str) -> list[Tool]:
        """Tools a skill grants to a worker spawned with it. No skill defines
        custom tools yet — scripts run through the worker's existing shell tool."""
        if name not in self._skills:
            raise SkillError(f"skill '{name}' not found")
        return []
