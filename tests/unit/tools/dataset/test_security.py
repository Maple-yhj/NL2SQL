from __future__ import annotations

import unittest

from pydantic import BaseModel, ConfigDict

from data_agent.dataset_query import DatasetQueryPlan
from data_agent.runtime.models import AgentMode
from data_agent.tools import ToolErrorCode, ToolInvoker
from data_agent.tools.providers.dataset.contracts import (
    ComputationSpec,
    QueryCompileInput,
    QueryRunInput,
)

from ._support import DatasetToolHarness, invoke


class _RejectingBroker:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self, *, grant, source):
        del grant, source
        self.calls += 1
        raise AssertionError("credential broker must not run for a denied tool")


class _ForgedCompile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan: DatasetQueryPlan
    source_id: str


class _RawQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str
    preview_rows: int = 20
    raw_sql: str


class _CodeCompute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: str
    artifact_id: str
    fields: tuple[str, ...]
    code: str


class DatasetToolSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.harness = DatasetToolHarness()
        self.plan = DatasetQueryPlan(
            analysis_type="detail",
            select=("dataset.orders.amount",),
            limit=2,
        )

    def tearDown(self) -> None:
        self.harness.close()

    async def test_modes_deny_credentials_and_full_execute_before_provider(self) -> None:
        rejecting = _RejectingBroker()
        _, _, plan_context = self.harness.invocation(
            AgentMode.PLAN,
            credential_broker=rejecting,
        )
        plan_invoker = ToolInvoker(self.harness.registry, credential_broker=rejecting)
        denied_plan = await invoke(
            plan_invoker,
            plan_context,
            call_id="plan-preview",
            tool_name="query.preview",
            input_data=QueryRunInput(artifact_id="artifact-" + "a" * 64),
        )

        _, preview_invoker, preview_context = self.harness.invocation(AgentMode.PREVIEW)
        denied_execute = await invoke(
            preview_invoker,
            preview_context,
            call_id="preview-execute",
            tool_name="query.execute",
            input_data=QueryRunInput(artifact_id="artifact-" + "a" * 64),
        )

        self.assertEqual(denied_plan.structured_error.code, ToolErrorCode.TOOL_NOT_ALLOWED)
        self.assertEqual(denied_execute.structured_error.code, ToolErrorCode.TOOL_NOT_ALLOWED)
        self.assertEqual(rejecting.calls, 0)

    async def test_cross_run_artifact_is_denied(self) -> None:
        _, owner_invoker, owner_context = self.harness.invocation(
            AgentMode.PREVIEW,
            run_id="owner-run",
        )
        compiled = await invoke(
            owner_invoker,
            owner_context,
            call_id="owner-compile",
            tool_name="query.compile",
            input_data=QueryCompileInput(plan=self.plan),
        )
        _, other_invoker, other_context = self.harness.invocation(
            AgentMode.PREVIEW,
            run_id="other-run",
        )
        denied = await invoke(
            other_invoker,
            other_context,
            call_id="other-preview",
            tool_name="query.preview",
            input_data=QueryRunInput(artifact_id=compiled.typed_data.artifact.artifact_id),
        )
        self.assertEqual(denied.structured_error.code, ToolErrorCode.ACCESS_DENIED)

    async def test_model_cannot_override_pins_or_supply_sql_or_code(self) -> None:
        _, invoker, context = self.harness.invocation(AgentMode.EXECUTE)
        forged = await invoke(
            invoker,
            context,
            call_id="forged-pins",
            tool_name="query.compile",
            input_data=_ForgedCompile(plan=self.plan, source_id="other-source"),
        )
        raw = await invoke(
            invoker,
            context,
            call_id="raw-sql",
            tool_name="query.preview",
            input_data=_RawQuery(
                artifact_id="artifact-" + "a" * 64,
                raw_sql="DROP TABLE public.orders",
            ),
        )
        code = await invoke(
            invoker,
            context,
            call_id="arbitrary-code",
            tool_name="analysis.compute",
            input_data=_CodeCompute(
                operation="describe",
                artifact_id="artifact-" + "a" * 64,
                fields=("amount",),
                code="import os",
            ),
        )
        self.assertEqual(forged.structured_error.code, ToolErrorCode.INPUT_INVALID)
        self.assertEqual(raw.structured_error.code, ToolErrorCode.INPUT_INVALID)
        self.assertEqual(code.structured_error.code, ToolErrorCode.INPUT_INVALID)

    async def test_relation_row_timeout_and_compute_dsl_are_bounded(self) -> None:
        _, relation_invoker, relation_context = self.harness.invocation(
            AgentMode.EXECUTE,
            allowed_relations=("public.customers",),
        )
        relation_denied = await invoke(
            relation_invoker,
            relation_context,
            call_id="relation-denied",
            tool_name="query.compile",
            input_data=QueryCompileInput(plan=self.plan),
        )
        _, row_invoker, row_context = self.harness.invocation(
            AgentMode.EXECUTE,
            max_rows=1,
            statement_timeout_ms=7,
        )
        row_denied = await invoke(
            row_invoker,
            row_context,
            call_id="row-denied",
            tool_name="query.compile",
            input_data=QueryCompileInput(plan=self.plan),
        )

        self.assertEqual(relation_denied.structured_error.code, ToolErrorCode.RELATION_NOT_ALLOWED)
        self.assertEqual(row_denied.structured_error.code, ToolErrorCode.ROW_LIMIT_EXCEEDED)
        with self.assertRaises(Exception):
            ComputationSpec(
                operation="moving_average",
                artifact_id="artifact-" + "a" * 64,
                fields=("amount",),
                parameters={"window": 101},
            )
        with self.assertRaises(Exception):
            ComputationSpec(
                operation="describe",
                artifact_id="artifact-" + "a" * 64,
                fields=("amount",),
                parameters={"python": "import os"},
            )


if __name__ == "__main__":
    unittest.main()
