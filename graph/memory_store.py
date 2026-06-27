from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

import asyncpg


MemoryContext = dict[str, list[dict[str, Any]]]
ConversationSession = dict[str, Any]
ConversationMessage = dict[str, Any]


@runtime_checkable
class ConversationStoreProtocol(Protocol):
    async def create_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        title: str = "",
    ) -> ConversationSession:
        ...

    async def list_conversations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        include_archived: bool = False,
    ) -> list[ConversationSession]:
        ...

    async def get_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession | None:
        ...

    async def update_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationSession | None:
        ...

    async def list_messages(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        ...

    async def load_context(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        limit: int,
    ) -> MemoryContext:
        ...

    async def save_turn(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        question: str,
        contextualized_question: str,
        sql: str,
        rows: list[dict[str, Any]],
        answer: str,
        ok: bool,
        error: str,
        trace: list[dict[str, Any]],
    ) -> None:
        ...

    async def upsert_user_memory(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_key: str,
        memory_value: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ...


class NullConversationStore:
    async def create_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        title: str = "",
    ) -> ConversationSession:
        now = _utc_now()
        return {
            "tenant_id": tenant_id,
            "conversation_id": str(uuid.uuid4()),
            "user_id": user_id,
            "title": title,
            "archived": False,
            "created_at": now,
            "updated_at": now,
        }

    async def list_conversations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        include_archived: bool = False,
    ) -> list[ConversationSession]:
        return []

    async def get_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession | None:
        return None

    async def update_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationSession | None:
        return None

    async def list_messages(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        return []

    async def load_context(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        limit: int,
    ) -> MemoryContext:
        return {"history": [], "user_memories": []}

    async def save_turn(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        question: str,
        contextualized_question: str,
        sql: str,
        rows: list[dict[str, Any]],
        answer: str,
        ok: bool,
        error: str,
        trace: list[dict[str, Any]],
    ) -> None:
        return None

    async def upsert_user_memory(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_key: str,
        memory_value: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], ConversationSession] = {}
        self._messages: list[dict[str, Any]] = []
        self._user_memories: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def create_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        title: str = "",
    ) -> ConversationSession:
        now = _utc_now()
        session = {
            "tenant_id": tenant_id,
            "conversation_id": str(uuid.uuid4()),
            "user_id": user_id,
            "title": title,
            "archived": False,
            "created_at": now,
            "updated_at": now,
        }
        self._sessions[(tenant_id, session["conversation_id"])] = deepcopy(session)
        return deepcopy(session)

    async def list_conversations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        include_archived: bool = False,
    ) -> list[ConversationSession]:
        sessions = [
            deepcopy(session)
            for session in self._sessions.values()
            if session["tenant_id"] == tenant_id
            and session["user_id"] == user_id
            and (include_archived or not session["archived"])
        ]
        sessions.sort(key=lambda item: item["updated_at"], reverse=True)
        return sessions[:limit]

    async def get_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession | None:
        session = self._sessions.get((tenant_id, conversation_id))
        if not session or session["user_id"] != user_id:
            return None
        return deepcopy(session)

    async def update_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationSession | None:
        session = self._sessions.get((tenant_id, conversation_id))
        if not session or session["user_id"] != user_id:
            return None
        if title is not None:
            session["title"] = title
        if archived is not None:
            session["archived"] = archived
        session["updated_at"] = _utc_now()
        return deepcopy(session)

    async def list_messages(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        if await self.get_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        ) is None:
            return []
        messages = [
            deepcopy(item)
            for item in self._messages
            if item["tenant_id"] == tenant_id
            and item["conversation_id"] == conversation_id
            and item["user_id"] == user_id
        ]
        if limit > 0:
            messages = messages[-limit:]
        return [
            {
                "role": item["role"],
                "content": item["content"],
                "metadata": deepcopy(item["metadata"]),
            }
            for item in messages
        ]

    async def load_context(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        limit: int,
    ) -> MemoryContext:
        history = [
            deepcopy(item)
            for item in self._messages
            if item["tenant_id"] == tenant_id and item["conversation_id"] == conversation_id
        ]
        if limit > 0:
            history = history[-limit:]

        memories = [
            deepcopy(value)
            for (memory_tenant_id, memory_user_id, _), value in self._user_memories.items()
            if memory_tenant_id == tenant_id and memory_user_id == user_id
        ]
        memories.sort(key=lambda item: item["memory_key"])
        return {
            "history": [
                {
                    "role": item["role"],
                    "content": item["content"],
                    "metadata": deepcopy(item["metadata"]),
                }
                for item in history
            ],
            "user_memories": memories,
        }

    async def save_turn(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        question: str,
        contextualized_question: str,
        sql: str,
        rows: list[dict[str, Any]],
        answer: str,
        ok: bool,
        error: str,
        trace: list[dict[str, Any]],
    ) -> None:
        if not conversation_id:
            return
        self._messages.append(
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "role": "user",
                "content": question,
                "metadata": {"contextualized_question": contextualized_question},
            }
        )
        session = self._sessions.get((tenant_id, conversation_id))
        if session:
            session["updated_at"] = _utc_now()
        self._messages.append(
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "role": "assistant",
                "content": answer or error or sql,
                "metadata": {
                    "sql": sql,
                    "rows": deepcopy(rows),
                    "answer": answer,
                    "ok": ok,
                    "error": error,
                    "trace": deepcopy(trace),
                },
            }
        )

    async def upsert_user_memory(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_key: str,
        memory_value: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not user_id:
            return
        self._user_memories[(tenant_id, user_id, memory_key)] = {
            "memory_key": memory_key,
            "memory_value": memory_value,
            "metadata": deepcopy(metadata or {}),
        }


class PostgresConversationStore:
    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn

    def _resolve_dsn(self) -> str:
        dsn = self.dsn or _memory_dsn_from_env()
        if not dsn:
            raise ValueError("Missing MEMORY_DATABASE_URL or MEMORY_POSTGRES_DSN for conversation memory.")
        return dsn

    async def _connect(self):
        return await asyncpg.connect(self._resolve_dsn(), ssl=False)

    async def create_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        title: str = "",
    ) -> ConversationSession:
        conversation_id = str(uuid.uuid4())
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO conversation_sessions
                    (tenant_id, conversation_id, user_id, title, archived, updated_at)
                VALUES ($1, $2, $3, $4, false, now())
                RETURNING tenant_id, conversation_id, user_id, title, archived, created_at, updated_at
                """,
                tenant_id,
                conversation_id,
                user_id,
                title,
            )
        finally:
            await conn.close()
        return _session_from_row(row)

    async def list_conversations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        include_archived: bool = False,
    ) -> list[ConversationSession]:
        conn = await self._connect()
        try:
            rows = await conn.fetch(
                """
                SELECT tenant_id, conversation_id, user_id, title, archived, created_at, updated_at
                FROM conversation_sessions
                WHERE tenant_id = $1
                  AND user_id = $2
                  AND ($3::boolean OR archived = false)
                ORDER BY updated_at DESC, id DESC
                LIMIT $4
                """,
                tenant_id,
                user_id,
                include_archived,
                max(limit, 0),
            )
        finally:
            await conn.close()
        return [_session_from_row(row) for row in rows]

    async def get_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                """
                SELECT tenant_id, conversation_id, user_id, title, archived, created_at, updated_at
                FROM conversation_sessions
                WHERE tenant_id = $1 AND user_id = $2 AND conversation_id = $3
                """,
                tenant_id,
                user_id,
                conversation_id,
            )
        finally:
            await conn.close()
        return _session_from_row(row) if row else None

    async def update_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationSession | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                """
                UPDATE conversation_sessions
                SET title = COALESCE($4, title),
                    archived = COALESCE($5, archived),
                    updated_at = now()
                WHERE tenant_id = $1 AND user_id = $2 AND conversation_id = $3
                RETURNING tenant_id, conversation_id, user_id, title, archived, created_at, updated_at
                """,
                tenant_id,
                user_id,
                conversation_id,
                title,
                archived,
            )
        finally:
            await conn.close()
        return _session_from_row(row) if row else None

    async def list_messages(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        conn = await self._connect()
        try:
            rows = await conn.fetch(
                """
                SELECT m.role, m.content, m.metadata
                FROM conversation_messages m
                JOIN conversation_sessions s
                  ON s.tenant_id = m.tenant_id
                 AND s.conversation_id = m.conversation_id
                WHERE m.tenant_id = $1
                  AND m.conversation_id = $2
                  AND s.user_id = $3
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT $4
                """,
                tenant_id,
                conversation_id,
                user_id,
                max(limit, 0),
            )
        finally:
            await conn.close()
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "metadata": _decode_json(row["metadata"]),
            }
            for row in reversed(rows)
        ]

    async def load_context(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        limit: int,
    ) -> MemoryContext:
        conn = await self._connect()
        try:
            rows = []
            if conversation_id:
                rows = await conn.fetch(
                    """
                    SELECT role, content, metadata
                    FROM conversation_messages
                    WHERE tenant_id = $1 AND conversation_id = $2
                    ORDER BY created_at DESC, id DESC
                    LIMIT $3
                    """,
                    tenant_id,
                    conversation_id,
                    max(limit, 0),
                )
            memory_rows = []
            if user_id:
                memory_rows = await conn.fetch(
                    """
                    SELECT memory_key, memory_value, metadata
                    FROM user_memories
                    WHERE tenant_id = $1 AND user_id = $2
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 20
                    """,
                    tenant_id,
                    user_id,
                )
        finally:
            await conn.close()

        history = [
            {
                "role": row["role"],
                "content": row["content"],
                "metadata": _decode_json(row["metadata"]),
            }
            for row in reversed(rows)
        ]
        user_memories = [
            {
                "memory_key": row["memory_key"],
                "memory_value": row["memory_value"],
                "metadata": _decode_json(row["metadata"]),
            }
            for row in memory_rows
        ]
        return {"history": history, "user_memories": user_memories}

    async def save_turn(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        user_id: str,
        question: str,
        contextualized_question: str,
        sql: str,
        rows: list[dict[str, Any]],
        answer: str,
        ok: bool,
        error: str,
        trace: list[dict[str, Any]],
    ) -> None:
        if not conversation_id:
            return
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO conversation_sessions (tenant_id, conversation_id, user_id, updated_at)
                VALUES ($1, $2, NULLIF($3, ''), now())
                ON CONFLICT (tenant_id, conversation_id)
                DO UPDATE SET user_id = COALESCE(EXCLUDED.user_id, conversation_sessions.user_id),
                              updated_at = now()
                """,
                tenant_id,
                conversation_id,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO conversation_messages
                    (tenant_id, conversation_id, user_id, role, content, metadata)
                VALUES ($1, $2, NULLIF($3, ''), 'user', $4, $5::jsonb)
                """,
                tenant_id,
                conversation_id,
                user_id,
                question,
                json.dumps({"contextualized_question": contextualized_question}, ensure_ascii=False),
            )
            await conn.execute(
                """
                INSERT INTO conversation_messages
                    (tenant_id, conversation_id, user_id, role, content, metadata)
                VALUES ($1, $2, NULLIF($3, ''), 'assistant', $4, $5::jsonb)
                """,
                tenant_id,
                conversation_id,
                user_id,
                answer or error or sql,
                json.dumps(
                    {
                        "sql": sql,
                        "rows": rows,
                        "answer": answer,
                        "ok": ok,
                        "error": error,
                        "trace": trace,
                    },
                    ensure_ascii=False,
                ),
            )
        finally:
            await conn.close()

    async def upsert_user_memory(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_key: str,
        memory_value: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not user_id:
            return
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO user_memories
                    (tenant_id, user_id, memory_key, memory_value, metadata, updated_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, now())
                ON CONFLICT (tenant_id, user_id, memory_key)
                DO UPDATE SET memory_value = EXCLUDED.memory_value,
                              metadata = EXCLUDED.metadata,
                              updated_at = now()
                """,
                tenant_id,
                user_id,
                memory_key,
                memory_value,
                json.dumps(metadata or {}, ensure_ascii=False),
            )
        finally:
            await conn.close()


def create_conversation_store(dsn: str | None = None) -> ConversationStoreProtocol:
    memory_dsn = dsn or _memory_dsn_from_env()
    if memory_dsn:
        return PostgresConversationStore(memory_dsn)
    return NullConversationStore()


def _memory_dsn_from_env() -> str | None:
    return os.getenv("MEMORY_DATABASE_URL") or os.getenv("MEMORY_POSTGRES_DSN")


def _decode_json(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_from_row(row: Any) -> ConversationSession:
    return {
        "tenant_id": row["tenant_id"],
        "conversation_id": row["conversation_id"],
        "user_id": row["user_id"] or "",
        "title": row["title"] or "",
        "archived": bool(row["archived"]),
        "created_at": _format_timestamp(row["created_at"]),
        "updated_at": _format_timestamp(row["updated_at"]),
    }


def _format_timestamp(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
