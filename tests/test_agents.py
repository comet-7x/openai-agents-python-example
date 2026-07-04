"""Orchestrator wiring: event publishing, the shared session workspace, and skill
discovery. No LLM turn is run — only construction and the synchronous paths."""

from __future__ import annotations

from comet import config
from comet.agents.orchestrator import OrchestratorAgent
from comet.display.view import SystemView
from comet.message_bus.bus import MessageBus
from comet.skills.registry import SkillsRegistry


async def test_orchestrator_publishes_to_view(model, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    bus = MessageBus()
    view = SystemView()
    bus.subscribe(view.apply)
    orch = OrchestratorAgent(model=model, bus=bus)

    orch.set_status("planning")
    orch.load_skill("xlsx")
    msg = await orch.spawn_subagent("analyst", skills=["xlsx"])
    sid = msg.split("id=")[1]

    assert view.orchestrator.status == "planning"
    assert view.orchestrator.loaded_skills == ["xlsx"]
    assert view.subagents[sid].name == "analyst"

    await orch.delete_subagent(sid)
    assert sid not in view.subagents
    orch._session.close()


async def test_shared_session_workspace(model, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "WORKSPACES_ROOT", tmp_path)
    orch = OrchestratorAgent(model=model)

    a = await orch._pool.create("A", "role A")
    b = await orch._pool.create("B", "role B")
    assert a.workspace_dir == b.workspace_dir == orch.session_workspace

    (a.workspace_dir / "report.md").write_text("# Draft\n", encoding="utf-8")
    assert (b.workspace_dir / "report.md").exists()  # sibling sees the deliverable

    await orch.delete_all()
    orch._session.close()


def test_skills_discovered() -> None:
    names = {s.name for s in SkillsRegistry(config.SKILLS_DIR).list_skills()}
    assert "xlsx" in names
