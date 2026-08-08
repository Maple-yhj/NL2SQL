from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from data_agent.memory.models import (
    MemoryBudget,
    MemoryBundle,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    ProposalStatus,
    RecordStatus,
    UserMemoryContent,
    UserMemoryOwner,
)
from data_agent.memory.providers.graph import (
    GraphRecallHit,
    GraphRetrievalAdapter,
)


NOW = datetime(2026, 7, 11, 6, 0, tzinfo=UTC)


def record(memory_id: str, value: str) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        owner_key="owner:user-a",
        owner=UserMemoryOwner(tenant_id="tenant-a", user_id="user-a"),
        content=UserMemoryContent(
            preference_key="report_style",
            preference_value=value,
        ),
        source="explicit_user_instruction",
        approval_status=ProposalStatus.COMMITTED,
        status=RecordStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        deduplication_key=f"dedup:{value}",
        proposal_id=f"proposal:{value}",
    )


class _Authority:
    def __init__(self) -> None:
        self.bundle = MemoryBundle(
            records=(record("memory:one", "concise"), record("memory:two", "brief")),
            used_tokens=20,
            used_characters=80,
            authority="postgres",
        )
        self.calls = 0

    async def recall(self, query, budget):
        self.calls += 1
        return self.bundle


class _Graph:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    async def search(self, query, budget):
        self.calls.append((query, budget))
        if self.fail:
            raise RuntimeError("graph unavailable")
        return (
            GraphRecallHit(memory_id="memory:graph-only", score=1.0),
            GraphRecallHit(memory_id="memory:two", score=0.9),
        )


def query() -> MemoryQuery:
    return MemoryQuery(
        tenant_id="tenant-a",
        user_id="user-a",
        scopes=(MemoryScope.USER,),
        query="report",
        as_of=NOW,
    )


class GraphRetrievalAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_can_only_rerank_postgres_authority_records(self) -> None:
        authority = _Authority()
        graph = _Graph()
        adapter = GraphRetrievalAdapter(authority=authority, graph=graph)

        result = await adapter.recall(query(), MemoryBudget())

        self.assertEqual(
            tuple(item.memory_id for item in result.records),
            ("memory:two", "memory:one"),
        )
        self.assertEqual(result.authority, "postgres")
        self.assertEqual(authority.calls, 1)
        self.assertEqual(len(graph.calls), 1)
        for forbidden in (
            "propose",
            "commit",
            "approve",
            "invalidate",
            "forget",
            "save_checkpoint",
        ):
            self.assertFalse(hasattr(adapter, forbidden), forbidden)

    async def test_graph_failure_returns_unchanged_authority_bundle(self) -> None:
        authority = _Authority()
        adapter = GraphRetrievalAdapter(authority=authority, graph=_Graph(fail=True))

        result = await adapter.recall(query(), MemoryBudget())

        self.assertIs(result, authority.bundle)


if __name__ == "__main__":
    unittest.main()
