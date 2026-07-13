from __future__ import annotations

import hashlib
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
DOMAIN_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
ENTERPRISE_ROOT = PROJECT_ROOT / "packs" / "enterprises" / "olist"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.runtime import (
    load_bundle_manifest,
    load_domain_pack,
    load_enterprise_binding,
)
from data_agent.runtime.models import PrincipalContext
from data_agent.runtime.binding import PreparedQuery
from data_agent.skills import logical_plan_from_eval_case
from data_agent.tools import (
    CatalogColumn,
    CatalogRelation,
    CatalogSnapshot,
    CredentialLease,
    ExplainResult,
    QueryRow,
    TabularResult,
    ToolBudget,
    ToolCall,
    ToolInvocationContext,
    ToolInvoker,
)
from data_agent.tools.providers import (
    BUILTIN_TOOL_NAMES,
    AnswerRenderInput,
    AnswerRenderOutput,
    DataInspectInput,
    DataInspectOutput,
    QueryCompileInput,
    QueryCompileOutput,
    QueryData,
    QueryExecuteInput,
    QueryExecutionOutput,
    QueryMode,
    ResultProfileInput,
    ResultProfileOutput,
    SemanticSearchInput,
    SemanticSearchOutput,
    build_builtin_registry,
)


class _FakeConnector:
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
                    columns=(
                        CatalogColumn(
                            name="seller_id",
                            data_type="text",
                            nullable=False,
                        ),
                    ),
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
        return self._table()

    @staticmethod
    def _table():
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
        table = self._table()
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


class _LeaseBroker:
    async def acquire(self, *, grant, source: str | None):
        now = datetime.now(UTC)
        return CredentialLease(
            credential_id="lease-test",
            grant_id=grant.grant_id,
            bundle_digest=grant.bundle_digest,
            source=source,
            connection_ref="secret://olist/local/database",
            capabilities=(grant.tool_name,),
            secret="postgresql://redacted",
            issued_at=now,
            expires_at=grant.expires_at,
        )


class BuiltinProviderTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.domain = load_domain_pack(DOMAIN_ROOT)
        cls.enterprise = load_enterprise_binding(ENTERPRISE_ROOT)
        cls.bundle = load_bundle_manifest(
            PROJECT_ROOT / "generated" / "bundles" / "olist-local.json",
            pack_lock=ENTERPRISE_ROOT / "pack.lock",
            schema_catalog=PROJECT_ROOT / "schema_catalog.json",
        )
        case = next(
            item for item in cls.domain.spec.evals if item.id == "commerce.metric_002"
        )
        cls.logical_plan = logical_plan_from_eval_case(case, cls.domain)
        cls.principal = PrincipalContext(
            tenant_id="seller-42",
            user_id="user-7",
            roles=("seller",),
        )

    def setUp(self) -> None:
        self.connector = _FakeConnector(self.bundle.schema_fingerprint)
        self.registry = build_builtin_registry(
            self.domain,
            self.enterprise,
            self.bundle,
            self.connector,
        )
        self.context = ToolInvocationContext(
            principal=self.principal,
            skill_id="commerce.analytics",
            skill_version="1.0.0",
            allowed_tools=BUILTIN_TOOL_NAMES,
            bundle=self.bundle,
            budget=ToolBudget(max_calls=20),
        )
        self.invoker = ToolInvoker(
            self.registry,
            credential_broker=_LeaseBroker(),
        )
        self.call_sequence = 0

    async def _invoke(self, name: str, payload):
        self.call_sequence += 1
        return await self.invoker.invoke(
            ToolCall(
                call_id=f"call-{self.call_sequence}",
                tool_name=name,
                tool_version="1.0.0",
                input_data=payload,
            ),
            self.context,
        )

    def test_builtin_registry_contains_exactly_six_consistent_manifests(self) -> None:
        self.assertEqual(
            BUILTIN_TOOL_NAMES,
            (
                "semantic.search",
                "data.inspect",
                "query.compile",
                "query.execute",
                "result.profile",
                "answer.render",
            ),
        )
        self.assertEqual(self.registry.names(), BUILTIN_TOOL_NAMES)
        self.assertTrue(self.registry.frozen)
        for spec in self.registry.specs():
            with self.subTest(tool=spec.name):
                provider = self.registry.provider(spec.name)
                self.assertIsNotNone(provider)
                self.assertEqual(provider.spec, spec)
                self.assertIs(provider.spec.input_schema, spec.input_schema)
                self.assertIs(provider.spec.output_schema, spec.output_schema)
                self.assertFalse(spec.input_schema.model_json_schema()["additionalProperties"])
                self.assertFalse(spec.output_schema.model_json_schema()["additionalProperties"])

    async def test_six_providers_form_a_typed_verified_pipeline(self) -> None:
        semantic = await self._invoke(
            "semantic.search",
            SemanticSearchInput(query="GMV", limit=5),
        )
        self.assertEqual(semantic.status, "success")
        self.assertIsInstance(semantic.typed_data, SemanticSearchOutput)
        self.assertIn(
            "commerce.gmv",
            [item.ref for item in semantic.typed_data.matches],
        )

        inspected = await self._invoke(
            "data.inspect",
            DataInspectInput(relations=("public.olist_order_items_dataset",)),
        )
        self.assertEqual(inspected.status, "success")
        self.assertIsInstance(inspected.typed_data, DataInspectOutput)
        self.assertEqual(len(self.connector.introspect_calls), 1)

        compiled = await self._invoke(
            "query.compile",
            QueryCompileInput(logical_plan=self.logical_plan),
        )
        self.assertEqual(compiled.status, "success", compiled.structured_error)
        self.assertIsInstance(compiled.typed_data, QueryCompileOutput)
        prepared = compiled.typed_data.prepared_query
        self.assertEqual(self.connector.explain_calls, [])
        self.assertEqual(self.connector.execute_calls, [])

        explained = await self._invoke(
            "query.execute",
            QueryExecuteInput(prepared_query=prepared, mode=QueryMode.EXPLAIN),
        )
        previewed = await self._invoke(
            "query.execute",
            QueryExecuteInput(
                prepared_query=prepared,
                mode=QueryMode.PREVIEW,
                preview_rows=2,
            ),
        )
        executed = await self._invoke(
            "query.execute",
            QueryExecuteInput(prepared_query=prepared, mode=QueryMode.EXECUTE),
        )
        self.assertIsInstance(explained.typed_data, QueryExecutionOutput)
        self.assertEqual(explained.typed_data.explain.estimated_cost, 2.5)
        self.assertEqual(len(previewed.typed_data.data.rows), 2)
        self.assertEqual(len(executed.typed_data.data.rows), 3)
        self.assertEqual(len(self.connector.preview_calls), 1)
        self.assertEqual(len(self.connector.execute_calls), 1)
        self.assertEqual(
            {
                item[0]
                for item in (
                    self.connector.explain_calls
                    + self.connector.execute_calls
                    + self.connector.preview_calls
                )
            },
            {prepared},
        )
        self.assertEqual(
            previewed.typed_data.data.policy_decision_id,
            prepared.policy_decision_id,
        )
        self.assertEqual(
            executed.typed_data.data.query_hash,
            prepared.sql_ast_hash,
        )
        self.assertTrue(executed.typed_data.data.verification_token)

        profile = await self._invoke(
            "result.profile",
            ResultProfileInput(data=executed.typed_data.data),
        )
        self.assertEqual(profile.status, "success", profile.structured_error)
        self.assertIsInstance(profile.typed_data, ResultProfileOutput)
        self.assertEqual(profile.typed_data.row_count, 3)
        gmv_profile = next(
            item for item in profile.typed_data.columns if item.name == "gmv"
        )
        self.assertEqual(gmv_profile.null_count, 1)
        self.assertEqual(gmv_profile.distinct_count, 2)

        rendered = await self._invoke(
            "answer.render",
            AnswerRenderInput(
                question="GMV 最高的卖家？",
                data=executed.typed_data.data,
                profile=profile.typed_data,
            ),
        )
        self.assertEqual(rendered.status, "success", rendered.structured_error)
        self.assertIsInstance(rendered.typed_data, AnswerRenderOutput)
        self.assertIn("3", rendered.typed_data.answer)
        self.assertIn("| seller_id | gmv |", rendered.typed_data.table_markdown)
        self.assertEqual(
            rendered.typed_data.evidence_query_hash,
            prepared.sql_ast_hash,
        )

    async def test_profile_and_answer_reject_unverified_policy_evidence(self) -> None:
        forged = QueryData(
            logical_plan_hash=self.logical_plan.stable_hash(),
            query_hash="0" * 64,
            policy_decision_id="policy-forged",
            verification_token="forged",
            columns=("value",),
            rows=(QueryRow(values=(1,)),),
        )

        profile = await self._invoke(
            "result.profile",
            ResultProfileInput(data=forged),
        )
        answer = await self._invoke(
            "answer.render",
            AnswerRenderInput(question="x", data=forged),
        )

        self.assertEqual(profile.status, "error")
        self.assertEqual(answer.status, "error")
        self.assertNotIn("policy-forged", profile.structured_error.message)
        self.assertNotIn("policy-forged", answer.structured_error.message)

    async def test_tampered_rows_and_caller_constructed_sql_are_not_trusted(self) -> None:
        compiled = await self._invoke(
            "query.compile",
            QueryCompileInput(logical_plan=self.logical_plan),
        )
        prepared = compiled.typed_data.prepared_query
        executed = await self._invoke(
            "query.execute",
            QueryExecuteInput(prepared_query=prepared, mode=QueryMode.EXECUTE),
        )
        genuine = executed.typed_data.data
        tampered = genuine.model_copy(
            update={"rows": (QueryRow(values=("attacker", 999999.0)),)}
        )

        profile = await self._invoke(
            "result.profile",
            ResultProfileInput(data=tampered),
        )
        answer = await self._invoke(
            "answer.render",
            AnswerRenderInput(question="x", data=tampered),
        )

        self.assertEqual(profile.status, "error")
        self.assertEqual(answer.status, "error")

        raw_sql = (
            'SELECT "t0"."seller_id" AS "seller_id" '
            'FROM "public"."olist_order_items_dataset" AS "t0" LIMIT 100'
        )
        raw = prepared.model_dump(mode="python")
        raw.update(
            executable_sql=raw_sql,
            logical_sql=raw_sql,
            sql_ast_hash=hashlib.sha256(raw_sql.encode("utf-8")).hexdigest(),
            parameters=(),
            allowed_relations=("public.olist_order_items_dataset",),
            max_rows=100,
        )
        caller_constructed = PreparedQuery.model_validate(raw)
        before = len(self.connector.execute_calls)

        rejected = await self._invoke(
            "query.execute",
            QueryExecuteInput(
                prepared_query=caller_constructed,
                mode=QueryMode.EXECUTE,
            ),
        )

        self.assertEqual(rejected.status, "error")
        self.assertEqual(len(self.connector.execute_calls), before)


if __name__ == "__main__":
    unittest.main()
