"""Ports implemented by authoritative and disabled memory providers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from data_agent.execution.contracts import ExecutionCheckpoint

from .models import (
    ApprovalContext,
    Checkpoint,
    ConversationRecord,
    ConversationWriteBatch,
    MemoryBudget,
    MemoryBundle,
    MemoryCandidate,
    MemoryQuery,
    MemorySelector,
    MessageRecord,
    ProposalId,
    SubjectScope,
)


@runtime_checkable
class MemoryManager(Protocol):
    async def recall(
        self,
        query: MemoryQuery,
        budget: MemoryBudget,
    ) -> MemoryBundle: ...

    async def propose(self, candidate: MemoryCandidate) -> ProposalId: ...

    async def commit(
        self,
        proposal_id: str,
        approval: ApprovalContext,
    ) -> None: ...

    async def invalidate(self, selector: MemorySelector) -> int: ...

    async def forget(self, subject: SubjectScope) -> int: ...

    async def save_checkpoint(self, run_id: str, state: Checkpoint) -> None: ...

    async def load_checkpoint(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
        run_id: str,
    ) -> ExecutionCheckpoint | None: ...

    async def create_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        title: str = "",
    ) -> ConversationRecord: ...

    async def get_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
    ) -> ConversationRecord | None: ...

    async def list_conversations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        limit: int,
        include_archived: bool = False,
    ) -> tuple[ConversationRecord, ...]: ...

    async def update_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationRecord | None: ...

    async def list_messages(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
        limit: int,
    ) -> tuple[MessageRecord, ...]: ...

    async def save_turn(self, batch: ConversationWriteBatch) -> None: ...


__all__ = ["MemoryManager"]
