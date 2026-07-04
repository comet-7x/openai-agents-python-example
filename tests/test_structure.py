"""Structural smoke tests: import graph, the broken-cycle guarantee, config paths,
and that the CLI actually launches as a module (which catches the agents/ name
collision that bites `python comet/cli.py`)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import comet
from comet import config

COMET = Path(comet.__file__).resolve().parent
REPO_ROOT = COMET.parent


def test_cli_imports() -> None:
    import comet.cli  # noqa: F401


def _imports(path: Path, module: str) -> bool:
    """True if the file has an actual import statement for `module` (ignores
    mentions in comments/docstrings)."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith((f"import {module}", f"from {module}")):
            return True
    return False


def test_no_agents_display_cycle() -> None:
    # display must not import agents, and agents must not import display.
    for p in (COMET / "display").glob("*.py"):
        assert not _imports(p, "comet.agents"), p
    for p in (COMET / "agents").glob("*.py"):
        assert not _imports(p, "comet.display"), p


def test_config_paths_anchored_to_repo_root() -> None:
    assert config.REPO_ROOT == REPO_ROOT
    assert config.SKILLS_DIR == REPO_ROOT / "skills"
    assert config.WORKSPACES_ROOT == REPO_ROOT / "workspaces"


def test_cli_runs_as_module() -> None:
    # No prompt / no flags -> usage line, and crucially it imports cleanly.
    result = subprocess.run(
        [sys.executable, "-m", "comet.cli"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert "No prompt provided" in result.stdout, result.stderr
