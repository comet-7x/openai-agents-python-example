import uuid
from pathlib import Path

from agents import OpenAIChatCompletionsModel, Tool

from comet.agents.worker import WorkerAgent
from comet.message_bus.bus import MessageBus


class WorkerAgentPool:
    """Pool of local worker agents managed by the orchestrator."""

    def __init__(
        self,
        model: OpenAIChatCompletionsModel,
        bus: MessageBus | None = None,
        session_workspace: Path | None = None,
    ) -> None:
        self._model = model
        self._bus = bus or MessageBus()
        # Shared workspace handed to every worker in this session (see WorkerAgent).
        self._session_workspace = session_workspace
        self._entries: dict[str, WorkerAgent] = {}

    async def create(
        self,
        agent_name: str,
        objective: str,
        extra_tools: list[Tool] | None = None,
    ) -> WorkerAgent:
        """Spawn a new WorkerAgent backed by local subprocess execution."""
        agent_id = "sub_" + str(uuid.uuid4()).replace("-", "")
        worker = WorkerAgent(
            agent_id,
            agent_name,
            self._model,
            objective,
            workspace_dir=self._session_workspace,
            extra_tools=extra_tools,
            bus=self._bus,
        )
        self._entries[agent_id] = worker
        return worker

    def get(self, agent_id: str) -> WorkerAgent | None:
        return self._entries.get(agent_id)

    async def delete(self, agent_id: str) -> None:
        worker = self._entries.pop(agent_id, None)
        if worker is not None:
            if worker.run_task is not None and not worker.run_task.done():
                worker.run_task.cancel()
            worker.conv_session.close()

    async def delete_all(self) -> None:
        for worker in self._entries.values():
            if worker.run_task is not None and not worker.run_task.done():
                worker.run_task.cancel()
            worker.conv_session.close()
        self._entries.clear()

    def list_entries(self) -> list[WorkerAgent]:
        return list(self._entries.values())
