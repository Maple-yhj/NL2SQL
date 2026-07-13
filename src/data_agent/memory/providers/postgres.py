"""PostgreSQL authority for memory, conversations, and checkpoints."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, Protocol

from data_agent.execution.contracts import ExecutionCheckpoint

from ..contracts import MemoryManager
from ..manager import (
    MemoryApprovalError,
    MemoryConflictError,
    MemoryProposalNotFoundError,
    MemoryStateError,
    _batch_artifact_references,
    _deduplication_key,
    _ensure_batch_owner_closure,
    _owner_key,
    _same_approval_decision,
    _stable_digest,
)
from ..models import (
    ApprovalContext,
    ApprovalDecision,
    ArtifactReference,
    Checkpoint,
    ConversationMemoryContent,
    ConversationMemoryOwner,
    ConversationRecord,
    ConversationStatus,
    ConversationWriteBatch,
    EnterpriseMemoryContent,
    EnterpriseMemoryOwner,
    EpisodicMemoryContent,
    EpisodicMemoryOwner,
    MemoryBudget,
    MemoryBundle,
    MemoryCandidate,
    MemoryContent,
    MemoryEvidence,
    MemoryOwner,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySelector,
    MessageRecord,
    ProposalId,
    ProposalStatus,
    RecordStatus,
    SafeMessagePayload,
    Sensitivity,
    SubjectScope,
    TrustLevel,
    UserMemoryContent,
    UserMemoryOwner,
    WorkingMemoryContent,
    WorkingMemoryOwner,
)
from ..policy import validate_candidate_content


class _ConnectionProtocol(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Any]: ...

    async def execute(self, query: str, *args: Any) -> str: ...

    async def fetchrow(self, query: str, *args: Any) -> Mapping[str, Any] | None: ...

    async def fetch(self, query: str, *args: Any) -> list[Mapping[str, Any]]: ...


class _PoolProtocol(Protocol):
    def acquire(self) -> AbstractAsyncContextManager[_ConnectionProtocol]: ...


class PostgresMemoryManager:
    """Authoritative PostgreSQL implementation with fail-closed owner predicates."""

    def __init__(
        self,
        pool: _PoolProtocol,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._pool = pool
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    async def create(
        cls,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
    ) -> "PostgresMemoryManager":
        import asyncpg

        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
        )
        return cls(pool)

    async def close(self) -> None:
        close = getattr(self._pool, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def create_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        title: str = "",
    ) -> ConversationRecord:
        now = self._now()
        conversation_id = str(uuid.uuid4())
        owner_key = _conversation_owner_key(
            tenant_id,
            user_id,
            domain_id,
            conversation_id,
        )
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                /* memory:create_conversation */
                INSERT INTO data_agent_conversations
                    (tenant_id, user_id, domain_id, conversation_id, owner_key,
                     title, summary, status, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, '', $7, $8, $8)
                RETURNING tenant_id, user_id, domain_id, conversation_id,
                          owner_key, title, summary, summary_run_id, status,
                          created_at, updated_at
                """,
                tenant_id,
                user_id,
                domain_id,
                conversation_id,
                owner_key,
                title,
                ConversationStatus.ACTIVE.value,
                now,
            )
        if row is None:
            raise MemoryStateError("conversation could not be created")
        return _conversation_from_row(row)

    async def get_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
    ) -> ConversationRecord | None:
        owner_key = _conversation_owner_key(
            tenant_id,
            user_id,
            domain_id,
            conversation_id,
        )
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                /* memory:get_conversation */
                SELECT tenant_id, user_id, domain_id, conversation_id,
                       owner_key, title, summary, summary_run_id, status,
                       created_at, updated_at
                FROM data_agent_conversations
                WHERE tenant_id = $1
                  AND owner_key IS NOT NULL
                  AND user_id = $2
                  AND domain_id = $3
                  AND conversation_id = $4
                  AND owner_key = $5
                """,
                tenant_id,
                user_id,
                domain_id,
                conversation_id,
                owner_key,
            )
        return _conversation_from_row(row) if row is not None else None

    async def list_conversations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        limit: int,
        include_archived: bool = False,
    ) -> tuple[ConversationRecord, ...]:
        if limit <= 0:
            return ()
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                /* memory:list_conversations */
                SELECT tenant_id, user_id, domain_id, conversation_id,
                       owner_key, title, summary, summary_run_id, status,
                       created_at, updated_at
                FROM data_agent_conversations
                WHERE tenant_id = $1
                  AND user_id = $2
                  AND domain_id = $3
                  AND ($4::boolean OR status = 'active')
                  AND owner_key IS NOT NULL
                ORDER BY updated_at DESC, conversation_id DESC
                LIMIT $5
                """,
                tenant_id,
                user_id,
                domain_id,
                include_archived,
                limit,
            )
        return tuple(
            _conversation_from_row(row)
            for row in rows
            if row.get("owner_key")
            == _conversation_owner_key(
                str(row["tenant_id"]),
                str(row["user_id"]),
                str(row["domain_id"]),
                str(row["conversation_id"]),
            )
        )

    async def update_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationRecord | None:
        owner_key = _conversation_owner_key(
            tenant_id,
            user_id,
            domain_id,
            conversation_id,
        )
        status_value = (
            None
            if archived is None
            else (
                ConversationStatus.ARCHIVED.value
                if archived
                else ConversationStatus.ACTIVE.value
            )
        )
        now = self._now()
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                /* memory:update_conversation_metadata */
                UPDATE data_agent_conversations
                SET title = COALESCE($6, title),
                    status = COALESCE($7, status),
                    updated_at = $8
                WHERE tenant_id = $1
                  AND user_id = $2
                  AND domain_id = $3
                  AND conversation_id = $4
                  AND owner_key = $5
                RETURNING tenant_id, user_id, domain_id, conversation_id,
                          owner_key, title, summary, summary_run_id, status,
                          created_at, updated_at
                """,
                tenant_id,
                user_id,
                domain_id,
                conversation_id,
                owner_key,
                title,
                status_value,
                now,
            )
        return _conversation_from_row(row) if row is not None else None

    async def list_messages(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
        limit: int,
    ) -> tuple[MessageRecord, ...]:
        if limit <= 0:
            return ()
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                /* memory:list_messages */
                SELECT m.message_id, m.tenant_id, m.user_id, m.domain_id,
                       m.conversation_id, m.run_id, m.owner_key, m.role,
                       m.content, m.safe_payload, m.created_at
                FROM data_agent_messages AS m
                JOIN data_agent_conversations AS c
                  ON c.tenant_id = m.tenant_id
                 AND c.user_id = m.user_id
                 AND c.domain_id = m.domain_id
                 AND c.conversation_id = m.conversation_id
                WHERE m.tenant_id = $1
                  AND m.user_id = $2
                  AND m.domain_id = $3
                  AND m.conversation_id = $4
                  AND m.owner_key IS NOT NULL
                ORDER BY m.created_at DESC, m.message_id DESC
                LIMIT $5
                """,
                tenant_id,
                user_id,
                domain_id,
                conversation_id,
                limit,
            )
        return tuple(reversed(tuple(_message_from_row(row) for row in rows)))

    async def save_turn(self, batch: ConversationWriteBatch) -> None:
        self._validate_batch(batch)
        now = self._now()
        conversation_owner_key = _conversation_owner_key(
            batch.tenant_id,
            batch.user_id,
            batch.domain_id,
            batch.conversation_id,
        )
        turn_owner_key = _turn_owner_key(
            batch.tenant_id,
            batch.user_id,
            batch.domain_id,
            batch.conversation_id,
            batch.run_id,
        )
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                owner = await connection.fetchrow(
                    """
                    /* memory:lock_conversation */
                    SELECT tenant_id, user_id, domain_id, conversation_id, owner_key
                    FROM data_agent_conversations
                    WHERE tenant_id = $1
                      AND user_id = $2
                      AND domain_id = $3
                      AND conversation_id = $4
                      AND owner_key = $5
                    FOR UPDATE
                    """,
                    batch.tenant_id,
                    batch.user_id,
                    batch.domain_id,
                    batch.conversation_id,
                    conversation_owner_key,
                )
                if owner is None:
                    raise PermissionError("conversation owner does not match")

                for message in (batch.user_message, batch.assistant_message):
                    message_id = "message:" + _stable_digest(
                        {
                            "tenant_id": batch.tenant_id,
                            "user_id": batch.user_id,
                            "domain_id": batch.domain_id,
                            "conversation_id": batch.conversation_id,
                            "run_id": batch.run_id,
                            "role": message.role.value,
                        }
                    )[:24]
                    await connection.execute(
                        """
                        /* memory:insert_message */
                        INSERT INTO data_agent_messages
                            (message_id, tenant_id, user_id, domain_id,
                             conversation_id, run_id, owner_key, role, content,
                             safe_payload, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                                $10::jsonb, $11)
                        ON CONFLICT (message_id) DO NOTHING
                        """,
                        message_id,
                        batch.tenant_id,
                        batch.user_id,
                        batch.domain_id,
                        batch.conversation_id,
                        batch.run_id,
                        turn_owner_key,
                        message.role.value,
                        message.content,
                        message.payload.model_dump_json(),
                        now,
                    )

                for reference in _batch_artifact_references(batch):
                    await self._insert_artifact(connection, reference, now)

                await connection.execute(
                    """
                    /* memory:update_conversation */
                    UPDATE data_agent_conversations
                    SET summary = $6, summary_run_id = $7, updated_at = $8
                    WHERE tenant_id = $1
                      AND user_id = $2
                      AND domain_id = $3
                      AND conversation_id = $4
                      AND owner_key = $5
                    """,
                    batch.tenant_id,
                    batch.user_id,
                    batch.domain_id,
                    batch.conversation_id,
                    conversation_owner_key,
                    batch.conversation_summary.summary,
                    batch.run_id,
                    now,
                )

                for memory_candidate in batch.proposals:
                    await self._propose_on_connection(connection, memory_candidate, now)

                if batch.checkpoint is not None:
                    await self._save_checkpoint_on_connection(
                        connection,
                        batch.run_id,
                        batch.checkpoint,
                        now,
                    )

    async def propose(self, candidate: MemoryCandidate) -> ProposalId:
        validate_candidate_content(candidate)
        now = self._now()
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                return await self._propose_on_connection(connection, candidate, now)

    async def _propose_on_connection(
        self,
        connection: _ConnectionProtocol,
        candidate: MemoryCandidate,
        now: datetime,
    ) -> ProposalId:
        validate_candidate_content(candidate)
        candidate_digest = _stable_digest(candidate)
        proposal_id = f"proposal:{candidate_digest[:24]}"
        deduplication_key = candidate.deduplication_key or _deduplication_key(
            candidate.owner,
            candidate.content,
        )
        owner_key = _owner_key(candidate.owner)
        owner = _owner_columns(candidate.owner)
        row = await connection.fetchrow(
            """
            /* memory:insert_proposal */
            INSERT INTO data_agent_memory_proposals
                (proposal_id, candidate_digest, owner_key, tenant_id, scope,
                 user_id, domain_id, conversation_id, run_id, candidate_json,
                 deduplication_key, status, proposed_at, updated_at)
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11,
                CASE WHEN EXISTS (
                    SELECT 1 FROM data_agent_memory_records AS r
                    WHERE r.owner_key = $3
                      AND r.tenant_id = $4
                      AND r.scope = $5
                      AND r.deduplication_key = $11
                      AND r.status IN ('active', 'pending_review')
                      AND r.content_json <> ($10::jsonb -> 'content')
                ) THEN 'conflict' ELSE $12 END,
                $13, $13
            )
            ON CONFLICT (owner_key, candidate_digest)
            DO UPDATE SET candidate_digest = EXCLUDED.candidate_digest
            RETURNING proposal_id, owner_key, status, candidate_json,
                      deduplication_key, tenant_id, user_id, domain_id,
                      conversation_id, run_id
            """,
            proposal_id,
            candidate_digest,
            owner_key,
            owner["tenant_id"],
            candidate.scope.value,
            owner["user_id"],
            owner["domain_id"],
            owner["conversation_id"],
            owner["run_id"],
            candidate.model_dump_json(),
            deduplication_key,
            ProposalStatus.PENDING_APPROVAL.value,
            now,
        )
        if row is None:
            raise MemoryStateError("memory proposal could not be persisted")
        return str(row["proposal_id"])

    async def commit(
        self,
        proposal_id: str,
        approval: ApprovalContext,
    ) -> None:
        safe_error = "proposal decision is not authorized or unavailable"
        conflict_after_commit = False
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    /* memory:lock_proposal */
                    SELECT proposal_id, owner_key, tenant_id, scope, status,
                           candidate_json, deduplication_key,
                           committed_memory_id, approver_user_id,
                           approver_roles, approval_decision,
                           approval_reason, decided_at
                    FROM data_agent_memory_proposals
                    WHERE proposal_id = $1 AND tenant_id = $2
                      AND owner_key IS NOT NULL
                    FOR UPDATE
                    """,
                    proposal_id,
                    approval.tenant_id,
                )
                if row is None:
                    raise MemoryApprovalError(safe_error)
                candidate = _candidate_from_json(row["candidate_json"])
                owner_key = _owner_key(candidate.owner)
                if (
                    str(row["owner_key"]) != owner_key
                    or not approval.authorizes(candidate.owner, candidate.content)
                ):
                    raise MemoryApprovalError(safe_error)
                status = ProposalStatus(str(row["status"]))
                if status in {ProposalStatus.COMMITTED, ProposalStatus.REJECTED}:
                    existing_approval = _approval_from_row(row)
                    if existing_approval is not None and _same_approval_decision(
                        existing_approval,
                        approval,
                    ):
                        return
                    raise MemoryApprovalError(safe_error)
                if status == ProposalStatus.CONFLICT:
                    raise MemoryConflictError(
                        "conflicting memory proposal cannot be committed"
                    )
                if status != ProposalStatus.PENDING_APPROVAL:
                    raise MemoryStateError(
                        f"proposal cannot be committed from {status.value}"
                    )
                if approval.decision == ApprovalDecision.REJECT:
                    await connection.execute(
                        """
                        /* memory:update_proposal_decision */
                        UPDATE data_agent_memory_proposals
                        SET status = $2, approver_user_id = $3,
                            approver_roles = $4::jsonb,
                            approval_decision = $5, approval_reason = $6,
                            decided_at = $7, updated_at = $7
                        WHERE proposal_id = $1 AND tenant_id = $8
                          AND owner_key = $9
                        """,
                        proposal_id,
                        ProposalStatus.REJECTED.value,
                        approval.approver_user_id,
                        _json(tuple(approval.roles)),
                        approval.decision.value,
                        approval.reason,
                        approval.decided_at,
                        approval.tenant_id,
                        owner_key,
                    )
                    return

                memory_id = f"memory:{_stable_digest({'proposal_id': proposal_id})[:24]}"
                owner = _owner_columns(candidate.owner)
                evidence_json = (
                    candidate.evidence.model_dump_json()
                    if candidate.evidence is not None
                    else None
                )
                inserted = await connection.fetchrow(
                    """
                    /* memory:insert_record */
                    INSERT INTO data_agent_memory_records
                        (memory_id, proposal_id, owner_key, tenant_id, scope,
                         user_id, domain_id, conversation_id, run_id,
                         content_json, source, evidence_json, trust_level,
                         approval_status, status, sensitivity, created_at,
                         updated_at, expires_at, domain_version, binding_version,
                         schema_fingerprint, deduplication_key)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb,
                            $11, $12::jsonb, $13, $14, $15, $16, $17, $17,
                            $18, $19, $20, $21, $22)
                    ON CONFLICT (owner_key, deduplication_key)
                    WHERE status IN ('active', 'pending_review')
                    DO NOTHING
                    RETURNING memory_id
                    """,
                    memory_id,
                    proposal_id,
                    owner_key,
                    owner["tenant_id"],
                    candidate.scope.value,
                    owner["user_id"],
                    owner["domain_id"],
                    owner["conversation_id"],
                    owner["run_id"],
                    candidate.content.model_dump_json(),
                    candidate.source,
                    evidence_json,
                    candidate.trust_level.value,
                    ProposalStatus.COMMITTED.value,
                    RecordStatus.ACTIVE.value,
                    candidate.sensitivity.value,
                    approval.decided_at,
                    candidate.expires_at,
                    candidate.versions.domain_version,
                    candidate.versions.binding_version,
                    candidate.versions.schema_fingerprint,
                    str(row["deduplication_key"]),
                )
                if inserted is None:
                    existing = await connection.fetchrow(
                        """
                        /* memory:lock_active_slot */
                        SELECT memory_id, owner_key, tenant_id, scope, user_id,
                               domain_id, conversation_id, run_id, content_json
                        FROM data_agent_memory_records
                        WHERE owner_key = $1 AND deduplication_key = $2
                          AND tenant_id = $3 AND scope = $4
                          AND user_id IS NOT DISTINCT FROM $5
                          AND domain_id IS NOT DISTINCT FROM $6
                          AND conversation_id IS NOT DISTINCT FROM $7
                          AND run_id IS NOT DISTINCT FROM $8
                          AND status IN ('active', 'pending_review')
                        FOR UPDATE
                        """,
                        owner_key,
                        str(row["deduplication_key"]),
                        owner["tenant_id"],
                        candidate.scope.value,
                        owner["user_id"],
                        owner["domain_id"],
                        owner["conversation_id"],
                        owner["run_id"],
                    )
                    if existing is None:
                        raise MemoryStateError(
                            "memory slot conflict could not be resolved"
                        )
                    existing_content = _content_from_json(existing["content_json"])
                    memory_id = str(existing["memory_id"])
                    if existing_content != candidate.content:
                        await connection.execute(
                            """
                            /* memory:update_proposal_conflict */
                            UPDATE data_agent_memory_proposals
                            SET status = $2, conflict_with = $3::jsonb,
                                approver_user_id = $4,
                                approver_roles = $5::jsonb,
                                approval_decision = $6,
                                decided_at = $7, updated_at = $7
                            WHERE proposal_id = $1 AND tenant_id = $8
                              AND owner_key = $9
                            """,
                            proposal_id,
                            ProposalStatus.CONFLICT.value,
                            _json((memory_id,)),
                            approval.approver_user_id,
                            _json(tuple(approval.roles)),
                            approval.decision.value,
                            approval.decided_at,
                            approval.tenant_id,
                            owner_key,
                        )
                        conflict_after_commit = True
                else:
                    memory_id = str(inserted["memory_id"])
                if not conflict_after_commit:
                    await connection.execute(
                        """
                        /* memory:update_proposal_committed */
                        UPDATE data_agent_memory_proposals
                        SET status = $2, committed_memory_id = $3,
                            approver_user_id = $4, approver_roles = $5::jsonb,
                            approval_decision = $6, approval_reason = $7,
                            decided_at = $8, updated_at = $8
                        WHERE proposal_id = $1 AND tenant_id = $9
                          AND owner_key = $10
                        """,
                        proposal_id,
                        ProposalStatus.COMMITTED.value,
                        memory_id,
                        approval.approver_user_id,
                        _json(tuple(approval.roles)),
                        approval.decision.value,
                        approval.reason,
                        approval.decided_at,
                        approval.tenant_id,
                        owner_key,
                    )
        if conflict_after_commit:
            raise MemoryConflictError(
                "conflicting memory proposal cannot be committed"
            )

    async def recall(
        self,
        query: MemoryQuery,
        budget: MemoryBudget,
    ) -> MemoryBundle:
        owner_keys = _query_owner_keys(query)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    /* memory:mark_version_drift */
                    UPDATE data_agent_memory_records
                    SET status = 'pending_review', updated_at = $10,
                        invalidation_reason = 'version_drift'
                    WHERE tenant_id = $1
                      AND owner_key = ANY($11::text[])
                      AND status = 'active'
                      AND (
                          (domain_version IS NOT NULL
                           AND domain_version IS DISTINCT FROM $7)
                       OR (binding_version IS NOT NULL
                           AND binding_version IS DISTINCT FROM $8)
                       OR (schema_fingerprint IS NOT NULL
                           AND schema_fingerprint IS DISTINCT FROM $9)
                      )
                      AND (
                          (scope IN ('working', 'conversation')
                           AND user_id = $2 AND domain_id = $3)
                       OR (scope = 'user' AND user_id = $2)
                       OR (scope IN ('episodic', 'enterprise')
                           AND domain_id = $3)
                      )
                      AND (conversation_id IS NULL OR conversation_id = $4)
                      AND (run_id IS NULL OR run_id = $5)
                      AND scope = ANY($6::text[])
                    """,
                    query.tenant_id,
                    query.user_id,
                    query.domain_id,
                    query.conversation_id,
                    query.run_id,
                    [scope.value for scope in query.scopes],
                    query.versions.domain_version,
                    query.versions.binding_version,
                    query.versions.schema_fingerprint,
                    query.as_of,
                    list(owner_keys),
                )
                rows = await connection.fetch(
                    """
                    /* memory:recall */
                    SELECT memory_id, proposal_id, owner_key, tenant_id, scope, user_id,
                           domain_id, conversation_id, run_id, content_json,
                           source, evidence_json, trust_level, approval_status,
                           status, sensitivity, created_at, updated_at, expires_at,
                           domain_version, binding_version, schema_fingerprint,
                           deduplication_key, invalidated_at, invalidation_reason
                    FROM data_agent_memory_records
                    WHERE tenant_id = $1
                      AND owner_key = ANY($12::text[])
                      AND approval_status = 'committed'
                      AND status = 'active'
                      AND (expires_at IS NULL OR expires_at > $10)
                      AND scope = ANY($6::text[])
                      AND (
                          (scope = 'working' AND user_id = $2
                           AND domain_id = $3 AND conversation_id = $4
                           AND run_id = $5)
                       OR (scope = 'conversation' AND user_id = $2
                           AND domain_id = $3 AND conversation_id = $4)
                       OR (scope = 'user' AND user_id = $2)
                       OR (scope IN ('episodic', 'enterprise')
                           AND domain_id = $3)
                      )
                      AND (domain_version IS NULL OR domain_version = $7)
                      AND (binding_version IS NULL OR binding_version = $8)
                      AND (schema_fingerprint IS NULL OR schema_fingerprint = $9)
                    ORDER BY updated_at DESC, memory_id DESC
                    LIMIT $11
                    """,
                    query.tenant_id,
                    query.user_id,
                    query.domain_id,
                    query.conversation_id,
                    query.run_id,
                    [scope.value for scope in query.scopes],
                    query.versions.domain_version,
                    query.versions.binding_version,
                    query.versions.schema_fingerprint,
                    query.as_of,
                    budget.max_records * 4,
                    list(owner_keys),
                )
        records = tuple(
            record
            for row in rows
            if (record := _record_from_row(row)) is not None
            and _query_owns_record(query, record)
            and _record_is_recallable(query, record)
            and _text_matches(query.query, record.content)
        )
        return _bundle_with_budget(records, budget)

    async def invalidate(self, selector: MemorySelector) -> int:
        if not _selector_authorized(selector):
            return 0
        now = self._now()
        async with self._pool.acquire() as connection:
            status = await connection.execute(
                """
                /* memory:invalidate */
                UPDATE data_agent_memory_records
                SET status = 'invalidated', updated_at = $10,
                    invalidated_at = $10, invalidation_reason = $9
                WHERE tenant_id = $1
                  AND ($2::text[] = '{}' OR memory_id = ANY($2::text[]))
                  AND ($3::text[] = '{}' OR scope = ANY($3::text[]))
                  AND ($4::text IS NULL OR user_id = $4)
                  AND ($5::text IS NULL OR domain_id = $5)
                  AND ($6::text IS NULL OR conversation_id = $6)
                  AND ($7::text IS NULL OR run_id = $7)
                  AND (
                      $8::boolean
                      OR (user_id = $4 AND scope NOT IN ('episodic', 'enterprise'))
                  )
                  AND status IN ('active', 'pending_review')
                """,
                selector.tenant_id,
                list(selector.memory_ids),
                [scope.value for scope in selector.scopes],
                selector.user_id,
                selector.domain_id,
                selector.conversation_id,
                selector.run_id,
                _is_admin(selector.actor_roles),
                selector.reason,
                now,
            )
        return _command_count(status)

    async def forget(self, subject: SubjectScope) -> int:
        if not (
            _is_admin(subject.actor_roles)
            or subject.actor_user_id == subject.user_id
        ):
            return 0
        total = 0
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                status = await connection.execute(
                    """
                    /* memory:forget_unlink_proposals */
                    UPDATE data_agent_memory_proposals
                    SET committed_memory_id = NULL, updated_at = now()
                    WHERE tenant_id = $1 AND domain_id = $2 AND user_id = $3
                      AND ($4::text IS NULL OR conversation_id = $4)
                      AND ($5::text IS NULL OR run_id = $5)
                    """,
                    subject.tenant_id,
                    subject.domain_id,
                    subject.user_id,
                    subject.conversation_id,
                    subject.run_id,
                )
                status = await connection.execute(
                    """
                    /* memory:forget_records */
                    DELETE FROM data_agent_memory_records
                    WHERE tenant_id = $1 AND domain_id = $2 AND user_id = $3
                      AND ($4::text IS NULL OR conversation_id = $4)
                      AND ($5::text IS NULL OR run_id = $5)
                    """,
                    subject.tenant_id,
                    subject.domain_id,
                    subject.user_id,
                    subject.conversation_id,
                    subject.run_id,
                )
                total += _command_count(status)
                status = await connection.execute(
                    """
                    /* memory:forget_proposals */
                    DELETE FROM data_agent_memory_proposals
                    WHERE tenant_id = $1 AND domain_id = $2 AND user_id = $3
                      AND ($4::text IS NULL OR conversation_id = $4)
                      AND ($5::text IS NULL OR run_id = $5)
                    """,
                    subject.tenant_id,
                    subject.domain_id,
                    subject.user_id,
                    subject.conversation_id,
                    subject.run_id,
                )
                total += _command_count(status)
                status = await connection.execute(
                    """
                    /* memory:forget_messages */
                    DELETE FROM data_agent_messages
                    WHERE tenant_id = $1 AND domain_id = $2 AND user_id = $3
                      AND ($4::text IS NULL OR conversation_id = $4)
                      AND ($5::text IS NULL OR run_id = $5)
                    """,
                    subject.tenant_id,
                    subject.domain_id,
                    subject.user_id,
                    subject.conversation_id,
                    subject.run_id,
                )
                total += _command_count(status)
                status = await connection.execute(
                    """
                    /* memory:forget_artifacts */
                    DELETE FROM data_agent_artifact_refs
                    WHERE tenant_id = $1 AND domain_id = $2 AND user_id = $3
                      AND ($4::text IS NULL OR conversation_id = $4)
                      AND ($5::text IS NULL OR run_id = $5)
                    """,
                    subject.tenant_id,
                    subject.domain_id,
                    subject.user_id,
                    subject.conversation_id,
                    subject.run_id,
                )
                total += _command_count(status)
                status = await connection.execute(
                    """
                    /* memory:forget_checkpoints */
                    DELETE FROM data_agent_checkpoints
                    WHERE tenant_id = $1 AND domain_id = $2 AND user_id = $3
                      AND ($4::text IS NULL OR conversation_id = $4)
                      AND ($5::text IS NULL OR run_id = $5)
                    """,
                    subject.tenant_id,
                    subject.domain_id,
                    subject.user_id,
                    subject.conversation_id,
                    subject.run_id,
                )
                total += _command_count(status)
                if subject.run_id is not None:
                    await connection.execute(
                        """
                        /* memory:forget_run_summary */
                        UPDATE data_agent_conversations
                        SET summary = '', summary_run_id = NULL, updated_at = now()
                        WHERE tenant_id = $1 AND domain_id = $2 AND user_id = $3
                          AND conversation_id = $4
                          AND summary_run_id = $5
                        """,
                        subject.tenant_id,
                        subject.domain_id,
                        subject.user_id,
                        subject.conversation_id,
                        subject.run_id,
                    )
                if subject.run_id is None:
                    status = await connection.execute(
                        """
                        /* memory:forget_conversations */
                        DELETE FROM data_agent_conversations
                        WHERE tenant_id = $1 AND domain_id = $2 AND user_id = $3
                          AND ($4::text IS NULL OR conversation_id = $4)
                        """,
                        subject.tenant_id,
                        subject.domain_id,
                        subject.user_id,
                        subject.conversation_id,
                    )
                    total += _command_count(status)
        return total

    async def save_checkpoint(self, run_id: str, state: Checkpoint) -> None:
        now = self._now()
        conversation_owner_key = _conversation_owner_key(
            state.tenant_id,
            state.user_id,
            state.domain_id,
            state.conversation_id,
        )
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                owner = await connection.fetchrow(
                    """
                    /* memory:lock_conversation */
                    SELECT tenant_id, user_id, domain_id, conversation_id, owner_key
                    FROM data_agent_conversations
                    WHERE tenant_id = $1 AND user_id = $2
                      AND domain_id = $3 AND conversation_id = $4
                      AND owner_key = $5
                    FOR UPDATE
                    """,
                    state.tenant_id,
                    state.user_id,
                    state.domain_id,
                    state.conversation_id,
                    conversation_owner_key,
                )
                if owner is None:
                    raise PermissionError("checkpoint owner does not match")
                await self._save_checkpoint_on_connection(
                    connection,
                    run_id,
                    state,
                    now,
                )

    async def _save_checkpoint_on_connection(
        self,
        connection: _ConnectionProtocol,
        run_id: str,
        state: Checkpoint,
        now: datetime,
    ) -> None:
        if run_id != state.run_id or state.checkpoint.state.run_id != run_id:
            raise ValueError("checkpoint run_id does not match its owner")
        validate_candidate_content(state.checkpoint)
        owner_key = _turn_owner_key(
            state.tenant_id,
            state.user_id,
            state.domain_id,
            state.conversation_id,
            state.run_id,
        )
        await connection.execute(
            """
            /* memory:save_checkpoint */
            INSERT INTO data_agent_checkpoints
                (tenant_id, user_id, domain_id, conversation_id, run_id,
                 owner_key, checkpoint_id, checkpoint_digest, checkpoint_json,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $10)
            ON CONFLICT
                (tenant_id, user_id, domain_id, conversation_id, run_id)
            DO UPDATE SET checkpoint_id = EXCLUDED.checkpoint_id,
                          checkpoint_digest = EXCLUDED.checkpoint_digest,
                          checkpoint_json = EXCLUDED.checkpoint_json,
                          updated_at = EXCLUDED.updated_at
            """,
            state.tenant_id,
            state.user_id,
            state.domain_id,
            state.conversation_id,
            state.run_id,
            owner_key,
            state.checkpoint.checkpoint_id,
            state.checkpoint.digest,
            state.checkpoint.model_dump_json(),
            now,
        )

    async def load_checkpoint(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
        run_id: str,
    ) -> ExecutionCheckpoint | None:
        owner_key = _turn_owner_key(
            tenant_id,
            user_id,
            domain_id,
            conversation_id,
            run_id,
        )
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                /* memory:load_checkpoint */
                SELECT checkpoint_json, checkpoint_digest, checkpoint_id, owner_key
                FROM data_agent_checkpoints
                WHERE tenant_id = $1 AND user_id = $2
                  AND domain_id = $3 AND conversation_id = $4
                  AND run_id = $5 AND owner_key = $6
                """,
                tenant_id,
                user_id,
                domain_id,
                conversation_id,
                run_id,
                owner_key,
            )
        if row is None:
            return None
        value = row["checkpoint_json"]
        checkpoint = (
            ExecutionCheckpoint.model_validate_json(value)
            if isinstance(value, (str, bytes, bytearray))
            else ExecutionCheckpoint.model_validate(value)
        )
        if (
            checkpoint.state.run_id != run_id
            or checkpoint.checkpoint_id != row["checkpoint_id"]
            or checkpoint.digest != row["checkpoint_digest"]
        ):
            raise ValueError("persisted checkpoint metadata does not match its payload")
        return checkpoint

    async def _insert_artifact(
        self,
        connection: _ConnectionProtocol,
        reference: ArtifactReference,
        now: datetime,
    ) -> None:
        owner_key = _turn_owner_key(
            reference.tenant_id,
            reference.user_id,
            reference.domain_id,
            reference.conversation_id,
            reference.run_id,
        )
        await connection.execute(
            """
            /* memory:insert_artifact */
            INSERT INTO data_agent_artifact_refs
                (artifact_id, tenant_id, user_id, domain_id, conversation_id,
                 run_id, owner_key, kind, digest, row_count, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT
                (tenant_id, user_id, domain_id, conversation_id, run_id,
                 artifact_id)
            DO NOTHING
            """,
            reference.artifact_id,
            reference.tenant_id,
            reference.user_id,
            reference.domain_id,
            reference.conversation_id,
            reference.run_id,
            owner_key,
            reference.kind,
            reference.digest,
            reference.row_count,
            now,
        )

    def _validate_batch(self, batch: ConversationWriteBatch) -> None:
        _ensure_batch_owner_closure(batch)
        validate_candidate_content(batch.user_message)
        validate_candidate_content(batch.assistant_message)
        validate_candidate_content(batch.conversation_summary)
        for reference in _batch_artifact_references(batch):
            validate_candidate_content(reference)
        for candidate in batch.proposals:
            validate_candidate_content(candidate)
        if batch.checkpoint is not None:
            validate_candidate_content(batch.checkpoint)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("memory clock must return a timezone-aware datetime")
        return value


def _conversation_owner_key(
    tenant_id: str,
    user_id: str,
    domain_id: str,
    conversation_id: str,
) -> str:
    return "conversation-owner:" + _stable_digest(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "domain_id": domain_id,
            "conversation_id": conversation_id,
        }
    )


def _turn_owner_key(
    tenant_id: str,
    user_id: str,
    domain_id: str,
    conversation_id: str,
    run_id: str,
) -> str:
    return "turn-owner:" + _stable_digest(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "domain_id": domain_id,
            "conversation_id": conversation_id,
            "run_id": run_id,
        }
    )


def _query_owner_keys(query: MemoryQuery) -> tuple[str, ...]:
    owners: list[MemoryOwner] = []
    for scope in query.scopes:
        if scope == MemoryScope.WORKING:
            owners.append(
                WorkingMemoryOwner(
                    tenant_id=query.tenant_id,
                    user_id=query.user_id,
                    domain_id=str(query.domain_id),
                    conversation_id=str(query.conversation_id),
                    run_id=str(query.run_id),
                )
            )
        elif scope == MemoryScope.CONVERSATION:
            owners.append(
                ConversationMemoryOwner(
                    tenant_id=query.tenant_id,
                    user_id=query.user_id,
                    domain_id=str(query.domain_id),
                    conversation_id=str(query.conversation_id),
                )
            )
        elif scope == MemoryScope.USER:
            owners.append(
                UserMemoryOwner(
                    tenant_id=query.tenant_id,
                    user_id=query.user_id,
                )
            )
        elif scope == MemoryScope.EPISODIC:
            owners.append(
                EpisodicMemoryOwner(
                    tenant_id=query.tenant_id,
                    domain_id=str(query.domain_id),
                )
            )
        else:
            owners.append(
                EnterpriseMemoryOwner(
                    tenant_id=query.tenant_id,
                    domain_id=str(query.domain_id),
                )
            )
    return tuple(_owner_key(owner) for owner in owners)


def _approval_from_row(row: Mapping[str, Any]) -> ApprovalContext | None:
    decision = row.get("approval_decision")
    approver = row.get("approver_user_id")
    decided_at = row.get("decided_at")
    if decision is None or approver is None or decided_at is None:
        return None
    roles_value = _json_value(row.get("approver_roles")) or []
    return ApprovalContext(
        tenant_id=str(row["tenant_id"]),
        approver_user_id=str(approver),
        roles=tuple(str(role) for role in roles_value),
        decision=ApprovalDecision(str(decision)),
        decided_at=decided_at,
        reason=(
            str(row["approval_reason"])
            if row.get("approval_reason") is not None
            else None
        ),
    )


def _candidate_from_json(value: Any) -> MemoryCandidate:
    if isinstance(value, (str, bytes, bytearray)):
        return MemoryCandidate.model_validate_json(value)
    return MemoryCandidate.model_validate(value)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _owner_columns(owner: MemoryOwner) -> dict[str, str | None]:
    return {
        "tenant_id": owner.tenant_id,
        "user_id": getattr(owner, "user_id", None),
        "domain_id": getattr(owner, "domain_id", None),
        "conversation_id": getattr(owner, "conversation_id", None),
        "run_id": getattr(owner, "run_id", None),
    }


def _owner_from_row(row: Mapping[str, Any]) -> MemoryOwner:
    scope = MemoryScope(str(row["scope"]))
    common = {"tenant_id": str(row["tenant_id"])}
    if scope == MemoryScope.WORKING:
        return WorkingMemoryOwner(
            **common,
            user_id=str(row["user_id"]),
            domain_id=str(row["domain_id"]),
            conversation_id=str(row["conversation_id"]),
            run_id=str(row["run_id"]),
        )
    if scope == MemoryScope.CONVERSATION:
        return ConversationMemoryOwner(
            **common,
            user_id=str(row["user_id"]),
            domain_id=str(row["domain_id"]),
            conversation_id=str(row["conversation_id"]),
        )
    if scope == MemoryScope.USER:
        return UserMemoryOwner(**common, user_id=str(row["user_id"]))
    if scope == MemoryScope.EPISODIC:
        return EpisodicMemoryOwner(**common, domain_id=str(row["domain_id"]))
    return EnterpriseMemoryOwner(**common, domain_id=str(row["domain_id"]))


def _content_from_json(value: Any) -> MemoryContent:
    raw = _json_value(value)
    scope = MemoryScope(str(raw["scope"]))
    model = {
        MemoryScope.WORKING: WorkingMemoryContent,
        MemoryScope.CONVERSATION: ConversationMemoryContent,
        MemoryScope.USER: UserMemoryContent,
        MemoryScope.EPISODIC: EpisodicMemoryContent,
        MemoryScope.ENTERPRISE: EnterpriseMemoryContent,
    }[scope]
    return model.model_validate(raw)


def _record_from_row(row: Mapping[str, Any]) -> MemoryRecord | None:
    try:
        evidence_value = _json_value(row.get("evidence_json"))
        return MemoryRecord(
            memory_id=str(row["memory_id"]),
            owner_key=str(row["owner_key"]),
            proposal_id=str(row["proposal_id"]),
            owner=_owner_from_row(row),
            content=_content_from_json(row["content_json"]),
            source=str(row["source"]),
            evidence=(
                MemoryEvidence.model_validate(evidence_value)
                if evidence_value is not None
                else None
            ),
            trust_level=TrustLevel(str(row["trust_level"])),
            approval_status=ProposalStatus(str(row["approval_status"])),
            status=RecordStatus(str(row["status"])),
            sensitivity=Sensitivity(str(row["sensitivity"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row.get("expires_at"),
            versions={
                "domain_version": row.get("domain_version"),
                "binding_version": row.get("binding_version"),
                "schema_fingerprint": row.get("schema_fingerprint"),
            },
            deduplication_key=str(row["deduplication_key"]),
            invalidated_at=row.get("invalidated_at"),
            invalidation_reason=row.get("invalidation_reason"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _conversation_from_row(row: Mapping[str, Any]) -> ConversationRecord:
    return ConversationRecord(
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        domain_id=str(row["domain_id"]),
        conversation_id=str(row["conversation_id"]),
        title=str(row.get("title") or ""),
        summary=str(row.get("summary") or ""),
        summary_run_id=(
            str(row["summary_run_id"])
            if row.get("summary_run_id") is not None
            else None
        ),
        status=ConversationStatus(str(row["status"])),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _message_from_row(row: Mapping[str, Any]) -> MessageRecord:
    payload = _json_value(row.get("safe_payload")) or {}
    return MessageRecord(
        message_id=str(row["message_id"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        domain_id=str(row["domain_id"]),
        conversation_id=str(row["conversation_id"]),
        run_id=str(row["run_id"]),
        role=str(row["role"]),
        content=str(row["content"]),
        payload=SafeMessagePayload.model_validate(payload),
        created_at=row["created_at"],
    )


def _query_owns_record(query: MemoryQuery, record: MemoryRecord) -> bool:
    owner = record.owner
    if owner.tenant_id != query.tenant_id or record.scope not in query.scopes:
        return False
    if record.scope in {
        MemoryScope.WORKING,
        MemoryScope.CONVERSATION,
        MemoryScope.USER,
    } and getattr(owner, "user_id", None) != query.user_id:
        return False
    if record.scope in {MemoryScope.WORKING, MemoryScope.CONVERSATION} and getattr(
        owner, "conversation_id", None
    ) != query.conversation_id:
        return False
    if record.scope == MemoryScope.WORKING and getattr(owner, "run_id", None) != query.run_id:
        return False
    if record.scope in {
        MemoryScope.WORKING,
        MemoryScope.CONVERSATION,
        MemoryScope.EPISODIC,
        MemoryScope.ENTERPRISE,
    } and getattr(
        owner, "domain_id", None
    ) != query.domain_id:
        return False
    return True


def _record_is_recallable(query: MemoryQuery, record: MemoryRecord) -> bool:
    if (
        record.status != RecordStatus.ACTIVE
        or record.approval_status != ProposalStatus.COMMITTED
        or (record.expires_at is not None and record.expires_at <= query.as_of)
    ):
        return False
    for field_name in ("domain_version", "binding_version", "schema_fingerprint"):
        pinned = getattr(record.versions, field_name)
        if pinned is not None and getattr(query.versions, field_name) != pinned:
            return False
    return True


def _text_matches(query: str, content: MemoryContent) -> bool:
    terms = {item.casefold() for item in query.split() if item.strip()}
    if not terms:
        return True
    rendered = content.model_dump_json().casefold()
    return any(term in rendered for term in terms)


def _bundle_with_budget(
    records: tuple[MemoryRecord, ...],
    budget: MemoryBudget,
) -> MemoryBundle:
    selected: list[MemoryRecord] = []
    characters = 0
    tokens = 0
    truncated = False
    for record in records:
        size = len(record.content.model_dump_json())
        record_tokens = max(1, (size + 3) // 4)
        if (
            len(selected) >= budget.max_records
            or characters + size > budget.max_characters
            or tokens + record_tokens > budget.max_tokens
        ):
            truncated = True
            continue
        selected.append(record)
        characters += size
        tokens += record_tokens
    return MemoryBundle(
        records=tuple(selected),
        used_tokens=tokens,
        used_characters=characters,
        truncated=truncated,
        authority="postgres",
    )


def _is_admin(roles: tuple[str, ...]) -> bool:
    normalized = {role.casefold() for role in roles}
    return bool(normalized & {"admin", "memory_admin", "enterprise_admin"})


def _selector_authorized(selector: MemorySelector) -> bool:
    if _is_admin(selector.actor_roles):
        return True
    if selector.user_id != selector.actor_user_id:
        return False
    return not any(
        scope in {MemoryScope.EPISODIC, MemoryScope.ENTERPRISE}
        for scope in selector.scopes
    )


def _command_count(status: str) -> int:
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (AttributeError, ValueError):
        return 0


assert isinstance(PostgresMemoryManager.__new__(PostgresMemoryManager), MemoryManager)


__all__ = ["PostgresMemoryManager"]
