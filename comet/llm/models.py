import asyncio
from collections.abc import AsyncIterator
from typing import Any

from agents import OpenAIChatCompletionsModel
from agents.items import ModelResponse, TResponseStreamEvent


class ConcurrencyLimitedModel(OpenAIChatCompletionsModel):
    """OpenAIChatCompletionsModel gated by a semaphore.

    Without this, N subagents running concurrently fire N simultaneous
    requests at the backend; if it can't actually serve that many at once,
    the excess just sit queued indefinitely with no status update, which is
    indistinguishable from a hung subagent in the live display.
    """

    def __init__(
        self,
        max_concurrency: int = 100,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        async with self._semaphore:
            return await super().get_response(*args, **kwargs)

    async def stream_response(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[TResponseStreamEvent]:
        async with self._semaphore:
            async for event in super().stream_response(*args, **kwargs):
                yield event
