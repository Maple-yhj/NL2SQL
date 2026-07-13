from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
DOMAIN_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
ENTERPRISE_ROOT = PROJECT_ROOT / "packs" / "enterprises" / "olist"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.execution import (
    COMMERCE_EXECUTION_GRAPH,
    ExecutionContext,
    ExecutionDependencies,
    ExecutionStatus,
    InternalGraphExecutor,
    ResolvedContext,
    VersionPin,
)
from data_agent.runtime import (
    load_bundle_manifest,
    load_domain_pack,
    load_enterprise_binding,
)
from data_agent.runtime.models import AgentMode, PrincipalContext
from data_agent.skills import logical_plan_from_eval_case
from data_agent.tools import CredentialLease, ToolInvoker
from data_agent.tools.providers import BUILTIN_TOOL_NAMES, build_builtin_registry
from tests.unit.tools.test_builtin_providers import _FakeConnector


class _Resolver:
    async def resolve(self, context):
        return ResolvedContext(contextualized_question=context.question)


class _Planner:
    def __init__(self, plan) -> None:
        self.plan = plan

    async def build_plan(self, **kwargs):
        return self.plan


class _CountingBroker:
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
            secret="postgresql://redacted",
            issued_at=now,
            expires_at=grant.expires_at,
        )


class ExecutionGraphIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        cls.plan = logical_plan_from_eval_case(case, cls.domain)
        cls.principal = PrincipalContext(
            tenant_id="seller-42",
            user_id="user-7",
            roles=("seller",),
        )

    def _context(self, mode: AgentMode) -> ExecutionContext:
        return ExecutionContext(
            run_id=f"integration-{mode.value}",
            mode=mode,
            question="Show GMV by seller",
            principal=self.principal,
            bundle=self.bundle,
            skill_id="commerce.analytics",
            skill_version="1.0.0",
            allowed_tools=BUILTIN_TOOL_NAMES,
            tool_versions=tuple(
                VersionPin(component=name, version="1.0.0")
                for name in BUILTIN_TOOL_NAMES
            ),
            model_versions=(
                VersionPin(component="planner", version="integration-v1"),
            ),
            preview_rows=2,
        )

    async def _run(self, mode: AgentMode):
        connector = _FakeConnector(self.bundle.schema_fingerprint)
        broker = _CountingBroker()
        registry = build_builtin_registry(
            self.domain,
            self.enterprise,
            self.bundle,
            connector,
        )
        invoker = ToolInvoker(registry, credential_broker=broker)
        executor = InternalGraphExecutor(
            COMMERCE_EXECUTION_GRAPH,
            ExecutionDependencies(
                invoker=invoker,
                context_resolver=_Resolver(),
                planner=_Planner(self.plan),
                domain_pack=self.domain,
            ),
        )
        result = await executor.execute(self._context(mode))
        return result, connector, broker

    async def test_three_modes_use_one_real_governed_tool_pipeline(self) -> None:
        plan, plan_connector, plan_broker = await self._run(AgentMode.PLAN)
        preview, preview_connector, preview_broker = await self._run(AgentMode.PREVIEW)
        execute, execute_connector, execute_broker = await self._run(AgentMode.EXECUTE)

        self.assertEqual(plan.state.status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(preview.state.status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(execute.state.status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(
            {
                plan.final_artifact.payload.query_hash,
                preview.final_artifact.payload.query_hash,
                execute.final_artifact.payload.query_hash,
            },
            {execute.final_artifact.payload.query_hash},
        )
        self.assertEqual(
            {
                plan.final_artifact.payload.policy_decision_id,
                preview.final_artifact.payload.policy_decision_id,
                execute.final_artifact.payload.policy_decision_id,
            },
            {execute.final_artifact.payload.policy_decision_id},
        )

        self.assertEqual(plan_broker.grants, [])
        self.assertEqual(plan_connector.explain_calls, [])
        self.assertEqual(plan_connector.preview_calls, [])
        self.assertEqual(plan_connector.execute_calls, [])

        self.assertEqual(len(preview_broker.grants), 2)
        self.assertEqual(len(preview_connector.explain_calls), 1)
        self.assertEqual(len(preview_connector.preview_calls), 1)
        self.assertEqual(preview_connector.execute_calls, [])
        self.assertEqual(preview.final_artifact.payload.row_count, 2)

        self.assertEqual(len(execute_broker.grants), 3)
        self.assertEqual(len(execute_connector.explain_calls), 1)
        self.assertEqual(len(execute_connector.preview_calls), 1)
        self.assertEqual(len(execute_connector.execute_calls), 1)
        self.assertEqual(execute.final_artifact.payload.row_count, 3)


if __name__ == "__main__":
    unittest.main()
