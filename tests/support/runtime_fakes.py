from __future__ import annotations

from datetime import UTC, datetime

from data_agent.memory import ConversationWriteBatch, MemoryBudget, NullMemoryManager
from data_agent.tools import (
    CatalogColumn,
    CatalogRelation,
    CatalogSnapshot,
    CredentialLease,
    ExplainResult,
    QueryRow,
    TabularResult,
)


class CountingMemory(NullMemoryManager):
    def __init__(self) -> None:
        super().__init__()
        self.recall_calls = 0
        self.saved: list[ConversationWriteBatch] = []

    async def recall(self, query, budget: MemoryBudget):
        self.recall_calls += 1
        return await super().recall(query, budget)

    async def save_turn(self, batch: ConversationWriteBatch) -> None:
        self.saved.append(batch)
        await super().save_turn(batch)


class CredentialBroker:
    def __init__(self) -> None:
        self.grants = []

    async def acquire(self, *, grant, source: str | None):
        self.grants.append(grant)
        now = datetime.now(UTC)
        return CredentialLease(
            credential_id=f"lease-{len(self.grants)}",
            grant_id=grant.grant_id,
            bundle_digest=grant.bundle_digest,
            source=source,
            connection_ref="secret://olist/local/database",
            capabilities=(grant.tool_name,),
            secret="redacted-lease",
            issued_at=now,
            expires_at=grant.expires_at,
        )


class FakeConnector:
    def __init__(self, schema_fingerprint: str) -> None:
        self.schema_fingerprint = schema_fingerprint
        self.introspect_calls = []
        self.explain_calls = []
        self.execute_calls = []
        self.preview_calls = []

    async def introspect_schema(self, grant, lease, *, relations=()):
        self.introspect_calls.append((grant, lease, tuple(relations)))
        return CatalogSnapshot(
            schema_fingerprint=self.schema_fingerprint,
            relations=(
                CatalogRelation(
                    relation="public.olist_order_items_dataset",
                    columns=(CatalogColumn(name="seller_id", data_type="text", nullable=False),),
                ),
            ),
        )

    async def explain(self, prepared, grant, lease):
        self.explain_calls.append((prepared, grant, lease))
        self._assert_bound(prepared, grant)
        return ExplainResult(
            plan_text='[{"Plan":{"Total Cost":2.5,"Plan Rows":3}}]',
            estimated_cost=2.5,
            estimated_rows=3,
        )

    async def execute_readonly(self, prepared, grant, lease):
        self.execute_calls.append((prepared, grant, lease))
        self._assert_bound(prepared, grant)
        return self.table()

    @staticmethod
    def table():
        return TabularResult(
            columns=("seller_id", "gmv"),
            rows=(
                QueryRow(values=("seller-a", 10.0)),
                QueryRow(values=("seller-b", 8.0)),
                QueryRow(values=("seller-c", None)),
            ),
        )

    async def preview(self, prepared, grant, lease, *, preview_rows: int):
        self.preview_calls.append((prepared, grant, lease, preview_rows))
        self._assert_bound(prepared, grant)
        table = self.table()
        return table.model_copy(
            update={
                "rows": table.rows[:preview_rows],
                "truncated": len(table.rows) > preview_rows,
            }
        )

    @staticmethod
    def _assert_bound(prepared, grant) -> None:
        if grant.policy_decision_id != prepared.policy_decision_id:
            raise AssertionError("policy decision drift")
        if grant.prepared_query_hash != prepared.sql_ast_hash:
            raise AssertionError("prepared query drift")
