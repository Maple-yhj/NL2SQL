"""Public runtime protocols used by product adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .events import AgentEvent
from .models import AgentRequest, PrincipalContext


@runtime_checkable
class DataAgentRuntime(Protocol):
    """Single application-service boundary for all Data Agent entrypoints."""

    def run(
        self,
        request: AgentRequest,
        principal: PrincipalContext,
    ) -> AsyncIterator[AgentEvent]: ...
