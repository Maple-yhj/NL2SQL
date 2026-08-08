"""Optional graph-assisted retrieval over authoritative memory records."""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from ..models import MemoryBudget, MemoryBundle, MemoryModel, MemoryQuery, NonBlankText


class GraphRecallHit(MemoryModel):
    memory_id: NonBlankText
    score: float = Field(ge=0.0, le=1.0)


class GraphRetriever(Protocol):
    async def search(
        self,
        query: MemoryQuery,
        budget: MemoryBudget,
    ) -> tuple[GraphRecallHit, ...]: ...


class AuthorityRecall(Protocol):
    async def recall(
        self,
        query: MemoryQuery,
        budget: MemoryBudget,
    ) -> MemoryBundle: ...


class GraphRetrievalAdapter:
    """Rerank authority rows without gaining write or approval capabilities."""

    def __init__(
        self,
        *,
        authority: AuthorityRecall,
        graph: GraphRetriever,
    ) -> None:
        self._authority = authority
        self._graph = graph

    async def recall(
        self,
        query: MemoryQuery,
        budget: MemoryBudget,
    ) -> MemoryBundle:
        authoritative = await self._authority.recall(query, budget)
        if not authoritative.records:
            return authoritative
        try:
            hits = await self._graph.search(query, budget)
        except Exception:
            return authoritative

        by_id = {record.memory_id: record for record in authoritative.records}
        ordered = []
        seen: set[str] = set()
        for hit in sorted(hits, key=lambda item: (-item.score, item.memory_id)):
            record = by_id.get(hit.memory_id)
            if record is None or hit.memory_id in seen:
                continue
            ordered.append(record)
            seen.add(hit.memory_id)
        ordered.extend(
            record
            for record in authoritative.records
            if record.memory_id not in seen
        )
        return authoritative.model_copy(update={"records": tuple(ordered)})


__all__ = [
    "AuthorityRecall",
    "GraphRecallHit",
    "GraphRetrievalAdapter",
    "GraphRetriever",
]
