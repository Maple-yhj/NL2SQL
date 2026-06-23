from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, Protocol, runtime_checkable

import asyncpg


MemoryContext = dict[str, list[dict[str, Any]]]


@runtime_checkable
class ConversationStoreProtocol(Protocol):
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
        self._messages: list[dict[str, Any]] = []
        self._user_memories: dict[tuple[str, str, str], dict[str, Any]] = {}

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
        self._messages.append(
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "role": "assistant",
                "content": answer or error or sql,
                "metadata": {
                    "sql": sql,
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
        dsn = self.dsn or os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")
        if not dsn:
            raise ValueError("Missing DATABASE_URL or POSTGRES_DSN for conversation memory.")
        return dsn

    async def _connect(self):
        return await asyncpg.connect(self._resolve_dsn(), ssl=False)

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
    if dsn or os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN"):
        return PostgresConversationStore(dsn)
    return NullConversationStore()


def _decode_json(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)
