"""Public runtime protocols used by product adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .events import AgentEvent
from .models import (
    AgentRequest,
    ConversationMessage,
    ConversationSummary,
    PrincipalContext,
)


@runtime_checkable
class DataAgentRuntime(Protocol):
    """Single application-service boundary for all Data Agent entrypoints."""

    def run(
        self,
        request: AgentRequest,
        principal: PrincipalContext,
    ) -> AsyncIterator[AgentEvent]: ...


@runtime_checkable
class ConversationRuntime(Protocol):
    """Public conversation facade implemented by the product Runtime."""

    async def create_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        title: str = "",
    ) -> ConversationSummary: ...

    async def list_conversations(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        limit: int,
        include_archived: bool = False,
    ) -> tuple[ConversationSummary, ...]: ...

    async def get_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
    ) -> ConversationSummary | None: ...

    async def update_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationSummary | None: ...

    async def list_conversation_messages(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
        limit: int,
    ) -> tuple[ConversationMessage, ...]: ...


@runtime_checkable
class ProductRuntime(DataAgentRuntime, ConversationRuntime, Protocol):
    """Combined protocol used by adapters that expose both product surfaces."""


__all__ = ["ConversationRuntime", "DataAgentRuntime", "ProductRuntime"]
