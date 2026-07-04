import asyncio
import uuid

from agents import OpenAIChatCompletionsModel

from subagent import SubAgent


class SubAgentPool:
    """Pool of local subagents managed by the orchestrator."""

    def __init__(self, model: OpenAIChatCompletionsModel) -> None:
        self._model = model
        self._entries: dict[str, SubAgent] = {}

    async def create(self, agent_name: str, objective: str) -> SubAgent:
        """Spawn a new SubAgent backed by local subprocess execution."""
        agent_id = "sub_" + str(uuid.uuid4()).replace("-", "")
        subagent = SubAgent(agent_id, agent_name, self._model, objective)
        self._entries[agent_id] = subagent
        return subagent

    def get(self, agent_id: str) -> SubAgent | None:
        return self._entries.get(agent_id)

    async def delete(self, agent_id: str) -> None:
        subagent = self._entries.pop(agent_id, None)
        if subagent is not None:
            if subagent.run_task is not None and not subagent.run_task.done():
                subagent.run_task.cancel()
            subagent.conv_session.close()

    async def delete_all(self) -> None:
        for subagent in self._entries.values():
            if subagent.run_task is not None and not subagent.run_task.done():
                subagent.run_task.cancel()
            subagent.conv_session.close()
        self._entries.clear()

    def list_entries(self) -> list[SubAgent]:
        return list(self._entries.values())
