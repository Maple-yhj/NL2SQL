"""Persistent product shell for deployments driven only by user datasets."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from data_agent.memory import MemoryManager, NullMemoryManager

from data_agent.model_client import ModelClient
from .errors import AgentError, ErrorCode
from .events import (
    AgentEvent,
    AgentEventType,
    RunFailedPayload,
    RunStartedPayload,
)
from .models import (
    AgentRequest,
    AgentResponse,
    ConversationMessage,
    ConversationMessageMetadata,
    ConversationSummary,
    PrincipalContext,
)


USER_DATASET_DOMAIN_ID = "dataset"
USER_DATASET_ENTERPRISE_ID = "user-dataset"
_T = TypeVar("_T")


class SQLiteConversationRepository:
    """Owner-scoped SQLite authority for conversation metadata and turns."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path).expanduser().resolve()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._initialize()

    async def create_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        title: str = "",
    ) -> ConversationSummary:
        self._require_dataset_domain(domain_id)
        now = datetime.now(UTC)
        conversation = ConversationSummary(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            domain_id=USER_DATASET_DOMAIN_ID,
            conversation_id=str(uuid4()),
            title=title.strip(),
            created_at=now,
            updated_at=now,
        )

        def operation(connection: sqlite3.Connection) -> ConversationSummary:
            connection.execute(
                """
                INSERT INTO conversations (
                    tenant_id, user_id, domain_id, conversation_id,
                    updated_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation.tenant_id,
                    conversation.user_id,
                    conversation.domain_id,
                    conversation.conversation_id,
                    conversation.updated_at.isoformat(),
                    conversation.model_dump_json(),
                ),
            )
            return conversation

        return await self._write(operation)

    async def list_conversations(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        limit: int,
        include_archived: bool = False,
    ) -> tuple[ConversationSummary, ...]:
        self._require_dataset_domain(domain_id)
        if limit < 1:
            raise ValueError("conversation limit must be positive")

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[ConversationSummary, ...]:
            rows = connection.execute(
                """
                SELECT payload
                FROM conversations
                WHERE tenant_id = ? AND user_id = ? AND domain_id = ?
                ORDER BY updated_at DESC, conversation_id DESC
                """,
                (
                    principal.tenant_id,
                    principal.user_id,
                    USER_DATASET_DOMAIN_ID,
                ),
            ).fetchall()
            conversations = tuple(
                ConversationSummary.model_validate_json(row[0])
                for row in rows
            )
            if include_archived:
                return conversations[:limit]
            return tuple(
                item for item in conversations if not item.archived
            )[:limit]

        return await self._read(operation)

    async def get_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
    ) -> ConversationSummary | None:
        self._require_dataset_domain(domain_id)
        return await self._get_conversation(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            conversation_id=conversation_id,
        )

    async def update_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationSummary | None:
        self._require_dataset_domain(domain_id)

        def operation(
            connection: sqlite3.Connection,
        ) -> ConversationSummary | None:
            current = self._conversation_on_connection(
                connection,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                conversation_id=conversation_id,
            )
            if current is None:
                return None
            updated = current.model_copy(
                update={
                    "title": current.title if title is None else title.strip(),
                    "archived": (
                        current.archived if archived is None else archived
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
            connection.execute(
                """
                UPDATE conversations
                SET payload = ?, updated_at = ?
                WHERE tenant_id = ? AND user_id = ?
                  AND domain_id = ? AND conversation_id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    principal.tenant_id,
                    principal.user_id,
                    USER_DATASET_DOMAIN_ID,
                    conversation_id,
                ),
            )
            return updated

        return await self._write(operation)

    async def list_conversation_messages(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        self._require_dataset_domain(domain_id)
        if limit < 1:
            raise ValueError("conversation message limit must be positive")

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[ConversationMessage, ...]:
            conversation = self._conversation_on_connection(
                connection,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                conversation_id=conversation_id,
            )
            if conversation is None:
                return ()
            rows = connection.execute(
                """
                SELECT payload
                FROM (
                    SELECT sequence, payload
                    FROM conversation_messages
                    WHERE tenant_id = ? AND user_id = ?
                      AND domain_id = ? AND conversation_id = ?
                    ORDER BY sequence DESC
                    LIMIT ?
                )
                ORDER BY sequence
                """,
                (
                    principal.tenant_id,
                    principal.user_id,
                    USER_DATASET_DOMAIN_ID,
                    conversation_id,
                    limit,
                ),
            ).fetchall()
            return tuple(
                ConversationMessage.model_validate_json(row[0])
                for row in rows
            )

        return await self._read(operation)

    async def record_conversation_turn(
        self,
        *,
        run_id: str,
        request: AgentRequest,
        principal: PrincipalContext,
        response: AgentResponse,
    ) -> None:
        if request.conversation_id is None:
            return
        user_message = ConversationMessage(
            role="user",
            content=request.question,
        )
        error = response.error
        assistant_message = ConversationMessage(
            role="assistant",
            content=(
                response.answer
                or (error.message if error is not None else None)
                or "分析未返回结果。"
            ),
            metadata=ConversationMessageMetadata(
                message_type=response.message_type,
                contextualized_question=response.contextualized_question,
                logical_plan=response.logical_plan,
                dataset_query_plan=response.dataset_query_plan,
                sql=response.sql,
                rows=response.rows,
                chart=response.chart,
                answer=response.answer,
                ok=response.ok,
                error=error,
                error_code=error.code.value if error is not None else None,
                row_count=len(response.rows),
                trace=response.trace,
                pending_memory_updates=response.pending_memory_updates,
                version_pins=response.version_pins,
                analysis_plan=response.analysis_plan,
                analysis_steps=response.analysis_steps,
                artifacts=response.artifacts,
                evidence=response.evidence,
                limitations=response.limitations,
            ),
        )

        def operation(connection: sqlite3.Connection) -> None:
            conversation = self._conversation_on_connection(
                connection,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                conversation_id=request.conversation_id or "",
            )
            if conversation is None:
                raise PermissionError("conversation is unavailable")
            existing = connection.execute(
                """
                SELECT role, payload
                FROM conversation_messages
                WHERE tenant_id = ? AND user_id = ?
                  AND domain_id = ? AND conversation_id = ? AND run_id = ?
                ORDER BY sequence
                """,
                (
                    principal.tenant_id,
                    principal.user_id,
                    USER_DATASET_DOMAIN_ID,
                    conversation.conversation_id,
                    run_id,
                ),
            ).fetchall()
            expected = (
                ("user", user_message.model_dump_json()),
                ("assistant", assistant_message.model_dump_json()),
            )
            if existing:
                if tuple((str(row[0]), str(row[1])) for row in existing) == expected:
                    return
                raise RuntimeError("conversation run already has different messages")
            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0)
                FROM conversation_messages
                WHERE tenant_id = ? AND user_id = ?
                  AND domain_id = ? AND conversation_id = ?
                """,
                (
                    principal.tenant_id,
                    principal.user_id,
                    USER_DATASET_DOMAIN_ID,
                    conversation.conversation_id,
                ),
            ).fetchone()
            sequence = int(row[0]) + 1 if row is not None else 1
            for message in (user_message, assistant_message):
                connection.execute(
                    """
                    INSERT INTO conversation_messages (
                        tenant_id, user_id, domain_id, conversation_id,
                        sequence, run_id, role, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        principal.tenant_id,
                        principal.user_id,
                        USER_DATASET_DOMAIN_ID,
                        conversation.conversation_id,
                        sequence,
                        run_id,
                        message.role,
                        message.model_dump_json(),
                    ),
                )
                sequence += 1
            updated = conversation.model_copy(
                update={"updated_at": datetime.now(UTC)}
            )
            connection.execute(
                """
                UPDATE conversations
                SET payload = ?, updated_at = ?
                WHERE tenant_id = ? AND user_id = ?
                  AND domain_id = ? AND conversation_id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    principal.tenant_id,
                    principal.user_id,
                    USER_DATASET_DOMAIN_ID,
                    conversation.conversation_id,
                ),
            )

        await self._write(operation)

    async def load_context_summary(
        self,
        *,
        request: AgentRequest,
        principal: PrincipalContext,
        max_messages: int = 8,
        max_characters: int = 4_096,
    ) -> str | None:
        """Render bounded prior turns without rows, SQL, traces, or run state."""

        if request.conversation_id is None:
            return None
        if max_messages < 1 or max_characters < 64:
            raise ValueError("conversation context budget is invalid")
        messages = await self.list_conversation_messages(
            principal=principal,
            domain_id=USER_DATASET_DOMAIN_ID,
            conversation_id=request.conversation_id,
            limit=max_messages,
        )
        lines: list[str] = []
        for message in messages:
            line = f"{message.role}: {message.content.strip()}"
            if message.role == "assistant" and message.metadata.evidence:
                refs = ", ".join(
                    f"{item.evidence_id}:{item.claim_key}:{item.artifact_id}"
                    for item in message.metadata.evidence
                )
                line += f" [evidence {refs}]"
            lines.append(line)
        rendered = "\n".join(lines)
        if not rendered:
            return None
        return rendered[-max_characters:]

    async def close(self) -> None:
        return None

    async def _get_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSummary | None:
        def operation(
            connection: sqlite3.Connection,
        ) -> ConversationSummary | None:
            return self._conversation_on_connection(
                connection,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )

        return await self._read(operation)

    @staticmethod
    def _require_dataset_domain(domain_id: str) -> None:
        if domain_id.strip() != USER_DATASET_DOMAIN_ID:
            raise ValueError("only the user dataset domain is available")

    @staticmethod
    def _conversation_on_connection(
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSummary | None:
        row = connection.execute(
            """
            SELECT payload
            FROM conversations
            WHERE tenant_id = ? AND user_id = ?
              AND domain_id = ? AND conversation_id = ?
            """,
            (
                tenant_id,
                user_id,
                USER_DATASET_DOMAIN_ID,
                conversation_id,
            ),
        ).fetchone()
        return (
            ConversationSummary.model_validate_json(row[0])
            if row is not None
            else None
        )

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (
                        tenant_id, user_id, domain_id, conversation_id
                    )
                );
                CREATE INDEX IF NOT EXISTS conversations_updated_idx
                ON conversations (
                    tenant_id, user_id, domain_id, updated_at DESC
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    run_id TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (
                        tenant_id, user_id, domain_id,
                        conversation_id, sequence
                    ),
                    FOREIGN KEY (
                        tenant_id, user_id, domain_id, conversation_id
                    )
                    REFERENCES conversations (
                        tenant_id, user_id, domain_id, conversation_id
                    )
                    ON DELETE CASCADE
                );
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(conversation_messages)"
                ).fetchall()
            }
            if "run_id" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_messages ADD COLUMN run_id TEXT"
                )
            if "role" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_messages ADD COLUMN role TEXT"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS conversation_messages_run_role_idx
                ON conversation_messages (
                    tenant_id, user_id, domain_id, conversation_id, run_id, role
                )
                WHERE run_id IS NOT NULL AND role IS NOT NULL
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def _read(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        async with self._lock:
            return await asyncio.to_thread(self._run_read, operation)

    async def _write(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        async with self._lock:
            return await asyncio.to_thread(self._run_write, operation)

    def _run_read(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        with closing(self._connect()) as connection, connection:
            return operation(connection)

    def _run_write(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return operation(connection)


class UploadDatasetRuntime:
    """No-datasource runtime delegating conversation duties to a repository."""

    def __init__(self, database_path: str | Path) -> None:
        self.repository = SQLiteConversationRepository(database_path)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.repository, name)

    async def create_conversation(self, **kwargs: Any) -> ConversationSummary:
        return await self.repository.create_conversation(**kwargs)

    async def list_conversations(self, **kwargs: Any) -> tuple[ConversationSummary, ...]:
        return await self.repository.list_conversations(**kwargs)

    async def get_conversation(self, **kwargs: Any) -> ConversationSummary | None:
        return await self.repository.get_conversation(**kwargs)

    async def update_conversation(self, **kwargs: Any) -> ConversationSummary | None:
        return await self.repository.update_conversation(**kwargs)

    async def list_conversation_messages(
        self,
        **kwargs: Any,
    ) -> tuple[ConversationMessage, ...]:
        return await self.repository.list_conversation_messages(**kwargs)

    async def record_conversation_turn(self, **kwargs: Any) -> None:
        await self.repository.record_conversation_turn(**kwargs)

    async def load_context_summary(self, **kwargs: Any) -> str | None:
        return await self.repository.load_context_summary(**kwargs)

    async def run(
        self,
        request: AgentRequest,
        principal: PrincipalContext,
    ) -> AsyncIterator[AgentEvent]:
        run_id = "upload-runtime-" + uuid4().hex
        yield AgentEvent(
            type=AgentEventType.RUN_STARTED,
            run_id=run_id,
            sequence=0,
            data=RunStartedPayload(
                mode=request.mode,
                enterprise_id=request.enterprise_id,
                domain_id=request.domain_id,
            ),
        )
        response = AgentResponse(
            ok=False,
            question=request.question,
            contextualized_question=request.question,
            conversation_id=request.conversation_id,
            tenant_id=principal.tenant_id,
            answer="请先上传数据集并激活语义绑定，再开始分析。",
            error=AgentError(
                code=ErrorCode.INVALID_REQUEST,
                message="请先上传数据集并激活语义绑定，再开始分析。",
            ),
        )
        if request.conversation_id is not None:
            conversation = await self.repository.get_conversation(
                principal=principal,
                domain_id=USER_DATASET_DOMAIN_ID,
                conversation_id=request.conversation_id,
            )
            if conversation is not None:
                await self.repository.record_conversation_turn(
                    run_id=run_id,
                    request=request,
                    principal=principal,
                    response=response,
                )
        yield AgentEvent(
            type=AgentEventType.RUN_FAILED,
            run_id=run_id,
            sequence=1,
            data=RunFailedPayload(error_code=ErrorCode.INVALID_REQUEST),
            response=response,
        )

    async def close(self) -> None:
        await self.repository.close()

@dataclass(frozen=True, slots=True)
class UploadRuntimeDependencies:
    model_client: ModelClient
    memory: MemoryManager


class UploadRuntimeComposition:
    def __init__(
        self,
        *,
        runtime: UploadDatasetRuntime,
        model_client: ModelClient,
        memory: MemoryManager | None = None,
    ) -> None:
        self.runtime = runtime
        self.dependencies = UploadRuntimeDependencies(
            model_client=model_client,
            memory=memory or NullMemoryManager(),
        )
        self._closed = False
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            await self.runtime.close()
            close = getattr(self.dependencies.model_client, "close", None)
            if close is not None:
                value = close()
                if inspect.isawaitable(value):
                    await value
            self._closed = True


__all__ = [
    "USER_DATASET_DOMAIN_ID",
    "USER_DATASET_ENTERPRISE_ID",
    "SQLiteConversationRepository",
    "UploadDatasetRuntime",
    "UploadRuntimeComposition",
    "UploadRuntimeDependencies",
]
