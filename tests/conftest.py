"""Shared fixtures for the comet smoke suite.

Tests here are offline: they construct agents, tools, and the bus, but never make
an LLM call, so no network or real credentials are needed.
"""

from __future__ import annotations

import pytest
from comet.llm.models import ConcurrencyLimitedModel
from openai import AsyncOpenAI


@pytest.fixture
def model() -> ConcurrencyLimitedModel:
    """A model object agents accept, wired to an unreachable backend. Fine because
    no test actually issues a request — we only exercise construction and events."""
    client = AsyncOpenAI(base_url="http://localhost:1", api_key="test", max_retries=0)
    return ConcurrencyLimitedModel(model="test-model", openai_client=client)
