from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class DataMemoryScope(str, Enum):
    GLOBAL = "global"
    USER = "user"
    CONVERSATION = "conversation"


@dataclass(frozen=True, slots=True)
class DataMemory:
    text: str
    scope: str
    source: str = ""
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DataMemoryStoreProtocol(Protocol):
    async def search(
        self,
        *,
        tenant_id: str,
        user_id: str = "",
        conversation_id: str = "",
        query: str,
        limit: int = 5,
    ) -> list[DataMemory]:
        ...

    async def add_episode(
        self,
        *,
        tenant_id: str,
        scope: DataMemoryScope | str,
        name: str,
        body: str | dict[str, Any] | list[Any],
        source_description: str,
        user_id: str = "",
        conversation_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ...


class NullDataMemoryStore:
    async def search(
        self,
        *,
        tenant_id: str,
        user_id: str = "",
        conversation_id: str = "",
        query: str,
        limit: int = 5,
    ) -> list[DataMemory]:
        return []

    async def add_episode(
        self,
        *,
        tenant_id: str,
        scope: DataMemoryScope | str,
        name: str,
        body: str | dict[str, Any] | list[Any],
        source_description: str,
        user_id: str = "",
        conversation_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None


class InMemoryDataMemoryStore:
    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []
        self._sequence = 0

    async def search(
        self,
        *,
        tenant_id: str,
        user_id: str = "",
        conversation_id: str = "",
        query: str,
        limit: int = 5,
    ) -> list[DataMemory]:
        groups = set(_allowed_group_ids(tenant_id, user_id, conversation_id))
        query_terms = _terms(query)
        matches: list[tuple[float, int, DataMemory]] = []
        for record in self._records:
            if record["group_id"] not in groups:
                continue
            memory = record["memory"]
            overlap = query_terms & _terms(memory.text)
            if query_terms and not overlap:
                continue
            score = len(overlap) / max(len(query_terms), 1)
            matches.append(
                (
                    score,
                    int(record["sequence"]),
                    DataMemory(
                        text=memory.text,
                        scope=memory.scope,
                        source=memory.source,
                        score=score,
                        metadata=deepcopy(memory.metadata),
                    ),
                )
            )
        matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if limit <= 0:
            return []
        return [memory for _, _, memory in matches[:limit]]

    async def add_episode(
        self,
        *,
        tenant_id: str,
        scope: DataMemoryScope | str,
        name: str,
        body: str | dict[str, Any] | list[Any],
        source_description: str,
        user_id: str = "",
        conversation_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized_scope = DataMemoryScope(scope)
        group_id = data_memory_group_id(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            scope=normalized_scope,
        )
        text = _episode_text(body)
        if not text:
            return None
        self._sequence += 1
        memory_metadata = deepcopy(metadata or {})
        if name:
            memory_metadata.setdefault("name", name)
        self._records.append(
            {
                "group_id": group_id,
                "sequence": self._sequence,
                "memory": DataMemory(
                    text=text,
                    scope=normalized_scope.value,
                    source=source_description,
                    metadata=memory_metadata,
                ),
            }
        )
        return None


class GraphitiDataMemoryStore:
    def __init__(
        self,
        *,
        neo4j_uri: str,
        neo4j_user: str,
        neo4j_password: str,
    ) -> None:
        self.neo4j_uri = neo4j_uri
        self.neo4j_user = neo4j_user
        self.neo4j_password = neo4j_password

    async def search(
        self,
        *,
        tenant_id: str,
        user_id: str = "",
        conversation_id: str = "",
        query: str,
        limit: int = 5,
    ) -> list[DataMemory]:
        if limit <= 0:
            return []
        graphiti = self._create_client()
        try:
            memories: list[DataMemory] = []
            for group_id in _allowed_group_ids(tenant_id, user_id, conversation_id):
                results = await self._search_group(
                    graphiti=graphiti,
                    query=query,
                    group_id=group_id,
                    limit=limit,
                )
                memories.extend(
                    _memory_from_graphiti_result(result, group_id)
                    for result in results
                )
            return memories[:limit]
        finally:
            await _close_graphiti(graphiti)

    async def add_episode(
        self,
        *,
        tenant_id: str,
        scope: DataMemoryScope | str,
        name: str,
        body: str | dict[str, Any] | list[Any],
        source_description: str,
        user_id: str = "",
        conversation_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        group_id = data_memory_group_id(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            scope=scope,
        )
        graphiti = self._create_client()
        try:
            await self._add_group_episode(
                graphiti=graphiti,
                group_id=group_id,
                name=name,
                body=body,
                source_description=source_description,
                metadata=metadata or {},
            )
        finally:
            await _close_graphiti(graphiti)

    def _create_client(self) -> Any:
        from graphiti_core import Graphiti

        return Graphiti(self.neo4j_uri, self.neo4j_user, self.neo4j_password)

    async def _search_group(
        self,
        *,
        graphiti: Any,
        query: str,
        group_id: str,
        limit: int,
    ) -> list[Any]:
        attempts = (
            ((), {"query": query, "group_id": group_id, "num_results": limit}),
            ((), {"query": query, "group_ids": [group_id], "num_results": limit}),
            ((), {"query": query, "group_id": group_id, "limit": limit}),
            ((), {"query": query, "group_ids": [group_id], "limit": limit}),
            ((query,), {"group_id": group_id, "num_results": limit}),
            ((query,), {"group_ids": [group_id], "num_results": limit}),
            ((query,), {"group_id": group_id, "limit": limit}),
            ((query,), {"group_ids": [group_id], "limit": limit}),
        )
        last_error: TypeError | None = None
        for args, kwargs in attempts:
            try:
                return list(await graphiti.search(*args, **kwargs))
            except TypeError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return []

    async def _add_group_episode(
        self,
        *,
        graphiti: Any,
        group_id: str,
        name: str,
        body: str | dict[str, Any] | list[Any],
        source_description: str,
        metadata: dict[str, Any],
    ) -> None:
        from graphiti_core.nodes import EpisodeType

        source = EpisodeType.text if isinstance(body, str) else EpisodeType.json
        episode_body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        await graphiti.add_episode(
            name=name,
            episode_body=episode_body,
            source=source,
            source_description=source_description,
            reference_time=datetime.now(timezone.utc),
            group_id=group_id,
            metadata=metadata,
        )


def create_data_memory_store() -> DataMemoryStoreProtocol:
    provider = os.getenv("DATA_MEMORY_PROVIDER", "").strip().lower()
    if not provider:
        return NullDataMemoryStore()
    if provider != "graphiti":
        return NullDataMemoryStore()

    neo4j_uri = os.getenv("GRAPHITI_NEO4J_URI", "").strip()
    neo4j_user = os.getenv("GRAPHITI_NEO4J_USER", "").strip()
    neo4j_password = os.getenv("GRAPHITI_NEO4J_PASSWORD", "").strip()
    if not all([neo4j_uri, neo4j_user, neo4j_password]):
        raise ValueError(
            "Graphiti data memory requires GRAPHITI_NEO4J_URI, "
            "GRAPHITI_NEO4J_USER, and GRAPHITI_NEO4J_PASSWORD."
        )
    return GraphitiDataMemoryStore(
        neo4j_uri=neo4j_uri,
        neo4j_user=neo4j_user,
        neo4j_password=neo4j_password,
    )


def data_memory_group_id(
    *,
    tenant_id: str,
    scope: DataMemoryScope | str,
    user_id: str = "",
    conversation_id: str = "",
) -> str:
    tenant = _required_text(tenant_id, "tenant_id")
    normalized_scope = DataMemoryScope(scope)
    if normalized_scope is DataMemoryScope.GLOBAL:
        return f"tenant:{tenant}:global"
    if normalized_scope is DataMemoryScope.USER:
        user = _required_text(user_id, "user_id")
        return f"tenant:{tenant}:user:{user}"
    conversation = _required_text(conversation_id, "conversation_id")
    return f"tenant:{tenant}:conversation:{conversation}"


def _required_text(value: str, name: str) -> str:
    stripped = str(value or "").strip()
    if not stripped:
        raise ValueError(f"{name} is required")
    return stripped


def format_data_memories(memories: list[DataMemory | dict[str, Any]] | None) -> str:
    lines: list[str] = []
    for item in memories or []:
        memory = data_memory_from_value(item)
        text = memory.text.strip()
        if not text:
            continue
        source = f" (source: {memory.source})" if memory.source else ""
        lines.append(f"- [{memory.scope}] {text}{source}")
    return "\n".join(lines)


def data_memory_to_dict(item: DataMemory | dict[str, Any]) -> dict[str, Any]:
    memory = data_memory_from_value(item)
    return {
        "text": memory.text,
        "scope": memory.scope,
        "source": memory.source,
        "score": memory.score,
        "metadata": deepcopy(memory.metadata),
    }


def extract_pending_memory_updates(
    *,
    question: str,
    contextualized_question: str,
    sql: str,
    answer: str,
    error: str,
) -> list[dict[str, Any]]:
    if str(error or "").strip():
        return []
    text = _explicit_memory_text(question) or _explicit_memory_text(contextualized_question)
    if not text:
        return []
    metadata: dict[str, Any] = {"requires_confirmation": True}
    if sql:
        metadata["sql"] = sql
    if answer:
        metadata["answer"] = answer
    return [
        {
            "scope": DataMemoryScope.USER.value,
            "text": text,
            "source": "explicit_user_instruction",
            "metadata": metadata,
        }
    ]


def _allowed_group_ids(
    tenant_id: str,
    user_id: str = "",
    conversation_id: str = "",
) -> list[str]:
    groups = [
        data_memory_group_id(
            tenant_id=tenant_id,
            scope=DataMemoryScope.GLOBAL,
        )
    ]
    if user_id:
        groups.append(
            data_memory_group_id(
                tenant_id=tenant_id,
                user_id=user_id,
                scope=DataMemoryScope.USER,
            )
        )
    if conversation_id:
        groups.append(
            data_memory_group_id(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                scope=DataMemoryScope.CONVERSATION,
            )
        )
    return groups


def _memory_from_graphiti_result(result: Any, group_id: str) -> DataMemory:
    metadata = {
        "group_id": group_id,
    }
    for key in ("uuid", "valid_at", "invalid_at"):
        value = getattr(result, key, None)
        if value is not None:
            metadata[key] = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return DataMemory(
        text=str(getattr(result, "fact", "") or getattr(result, "text", "") or ""),
        scope=_scope_from_group_id(group_id),
        source="graphiti",
        score=_optional_float(getattr(result, "score", None)),
        metadata=metadata,
    )


async def _close_graphiti(graphiti: Any) -> None:
    close = getattr(graphiti, "close", None)
    if close is None:
        return
    result = close()
    if hasattr(result, "__await__"):
        await result


def _scope_from_group_id(group_id: str) -> str:
    if ":conversation:" in group_id:
        return DataMemoryScope.CONVERSATION.value
    if ":user:" in group_id:
        return DataMemoryScope.USER.value
    return DataMemoryScope.GLOBAL.value


def data_memory_from_value(item: DataMemory | dict[str, Any]) -> DataMemory:
    if isinstance(item, DataMemory):
        return item
    return DataMemory(
        text=str(item.get("text") or ""),
        scope=str(item.get("scope") or ""),
        source=str(item.get("source") or ""),
        score=item.get("score"),
        metadata=deepcopy(item.get("metadata") or {}),
    )


def _episode_text(body: str | dict[str, Any] | list[Any]) -> str:
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, dict):
        text = str(body.get("text") or "").strip()
        if text:
            return text
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _terms(value: str) -> set[str]:
    return {term.lower() for term in re.findall(r"\w+", value)}


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _explicit_memory_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    remember_zh = "\u8bb0\u4f4f"
    please_remember_zh = "\u8bf7\u8bb0\u4f4f"
    future_default_zh = "(?:\u4ee5\u540e|\u540e\u7eed)\u9ed8\u8ba4"
    zh_punctuation = ":\uff1a\uff0c,\u3001-"
    patterns = (
        r"(?is)^\s*remember\b\s*[:\uff1a,-]?\s*(.+)$",
        r"(?is)^\s*please\s+remember\b\s*[:\uff1a,-]?\s*(.+)$",
        r"(?is)^\s*save\s+this\b\s*[:\uff1a,-]?\s*(.+)$",
        r"(?is)^\s*from\s+now\s+on\b\s*[:\uff1a,-]?\s*(.+)$",
        r"(?is)^\s*default\s+to\b\s*[:\uff1a,-]?\s*(.+)$",
        f"(?s)^\\s*{remember_zh}\\s*[{zh_punctuation}]?\\s*(.+)$",
        f"(?s)^\\s*{please_remember_zh}\\s*[{zh_punctuation}]?\\s*(.+)$",
        f"(?s)^\\s*{future_default_zh}\\s*[{zh_punctuation}]?\\s*(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, text)
        if match:
            return match.group(1).strip()
    return ""
