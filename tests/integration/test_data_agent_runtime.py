from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.execution import (
    COMMERCE_EXECUTION_GRAPH,
    ExecutionDependencies,
    InternalGraphExecutor,
)
from data_agent.memory import ConversationWriteBatch, MemoryBudget, NullMemoryManager
from data_agent.runtime.bundle_store import BundlePaths, BundleStore
from data_agent.runtime.context import ContextAssembler
from data_agent.runtime.events import AgentEventType
from data_agent.runtime.models import AgentMode, AgentRequest, PrincipalContext
from data_agent.runtime.service import DefaultDataAgentRuntime
from data_agent.skills import BUILTIN_SKILL_REGISTRY, logical_plan_from_eval_case
from data_agent.tools import CredentialLease, ToolInvoker
from data_agent.tools.providers import BUILTIN_TOOL_NAMES, build_builtin_registry
from tests.unit.tools.test_builtin_providers import _FakeConnector


class _CountingMemory(NullMemoryManager):
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


class _Planner:
    def __init__(self, plan) -> None:
        self.plan = plan

    async def build_plan(self, **kwargs):
        return self.plan


class _Model:
    model_id = "planner"
    version = "integration-v1"

    async def complete(self, prompt: str, **kwargs) -> str:
        return self.plan.model_dump_json()


class _Broker:
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


class DataAgentRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = BundlePaths(
            domain_root=ROOT / "packs" / "domains" / "commerce",
            enterprise_root=ROOT / "packs" / "enterprises" / "olist",
            deployment_profile=ROOT / "packs" / "deployments" / "olist-local.yaml",
            pack_lock=ROOT / "packs" / "enterprises" / "olist" / "pack.lock",
            schema_catalog=ROOT / "schema_catalog.json",
            bundle_manifest=ROOT / "generated" / "bundles" / "olist-local.json",
        )
        cls.store = BundleStore()
        cls.snapshot = cls.store.load_and_activate(cls.paths)
        case = next(
            item
            for item in cls.snapshot.domain_pack.spec.evals
            if item.id == "commerce.metric_002"
        )
        cls.plan = logical_plan_from_eval_case(case, cls.snapshot.domain_pack)
        cls.principal = PrincipalContext(
            tenant_id="seller-42",
            user_id="user-7",
            roles=("seller",),
        )

    async def _run(self, mode: AgentMode):
        from data_agent.runtime.context_resolver import RuntimeContextResolver
        from data_agent.runtime.dependencies import RuntimeDependencies

        memory = _CountingMemory()
        conversation = await memory.create_conversation(
            tenant_id=self.principal.tenant_id,
            user_id=self.principal.user_id,
            domain_id="commerce",
        )
        connector = _FakeConnector(self.snapshot.bundle.schema_fingerprint)
        broker = _Broker()
        registry = build_builtin_registry(
            self.snapshot.domain_pack,
            self.snapshot.enterprise_binding,
            self.snapshot.bundle,
            connector,
        )
        invoker = ToolInvoker(registry, credential_broker=broker)
        resolver = RuntimeContextResolver(
            memory=memory,
            assembler=ContextAssembler(),
        )
        planner = _Planner(self.plan)
        executor = InternalGraphExecutor(
            COMMERCE_EXECUTION_GRAPH,
            ExecutionDependencies(
                invoker=invoker,
                context_resolver=resolver,
                planner=planner,
                domain_pack=self.snapshot.domain_pack,
            ),
        )
        run_store = BundleStore()
        run_store.load_and_activate(self.paths)
        runtime = DefaultDataAgentRuntime(
            RuntimeDependencies(
                bundle_store=run_store,
                skill_registry=BUILTIN_SKILL_REGISTRY,
                tool_registry=registry,
                graph=COMMERCE_EXECUTION_GRAPH,
                executor=executor,
                memory=memory,
                context_resolver=resolver,
                planner=planner,
                model_client=_Model(),
                run_id_factory=lambda: f"runtime-{mode.value}",
            )
        )
        events = [
            event
            async for event in runtime.run(
                AgentRequest(
                    question="Show GMV by seller",
                    conversation_id=conversation.conversation_id,
                    mode=mode,
                ),
                self.principal,
            )
        ]
        return events, connector, broker, memory, executor

    async def test_three_modes_use_the_same_real_graph_and_governed_tools(self) -> None:
        plan = await self._run(AgentMode.PLAN)
        preview = await self._run(AgentMode.PREVIEW)
        execute = await self._run(AgentMode.EXECUTE)

        for mode, run in zip(AgentMode, (plan, preview, execute), strict=True):
            events, _connector, _broker, memory, executor = run
            self.assertEqual([event.sequence for event in events], [0, 1, 2])
            self.assertEqual(events[-1].type, AgentEventType.RUN_COMPLETED)
            self.assertTrue(events[-1].response.ok)
            self.assertIs(executor.graph, COMMERCE_EXECUTION_GRAPH)
            self.assertEqual(memory.recall_calls, 1)
            self.assertIsNotNone(memory.saved[0].checkpoint)
            self.assertIn(
                "semantic.search",
                [trace.node for trace in memory.saved[0].assistant_message.payload.trace],
            )
            self.assertEqual(events[-1].response.trace, ())
            self.assertEqual(events[-1].response.logical_plan, self.plan)
            self.assertEqual(events[-1].response.conversation_id, memory.saved[0].conversation_id)
            self.assertEqual(events[-1].response.tenant_id, self.principal.tenant_id)
            self.assertEqual(events[-1].response.error, None)

        plan_events, plan_connector, plan_broker, *_ = plan
        preview_events, preview_connector, preview_broker, *_ = preview
        execute_events, execute_connector, execute_broker, *_ = execute

        self.assertEqual(plan_broker.grants, [])
        self.assertEqual(plan_connector.explain_calls, [])
        self.assertEqual(plan_events[-1].response.rows, ())
        self.assertIsNotNone(plan_events[-1].response.sql)

        self.assertEqual(len(preview_broker.grants), 2)
        self.assertEqual(len(preview_connector.explain_calls), 1)
        self.assertEqual(len(preview_connector.preview_calls), 1)
        self.assertEqual(preview_connector.execute_calls, [])
        self.assertEqual(len(preview_events[-1].response.rows), 3)

        self.assertEqual(len(execute_broker.grants), 3)
        self.assertEqual(len(execute_connector.explain_calls), 1)
        self.assertEqual(len(execute_connector.preview_calls), 1)
        self.assertEqual(len(execute_connector.execute_calls), 1)
        self.assertEqual(len(execute_events[-1].response.rows), 3)
        self.assertEqual(
            {
                plan_events[-1].response.sql,
                preview_events[-1].response.sql,
                execute_events[-1].response.sql,
            },
            {execute_events[-1].response.sql},
        )


if __name__ == "__main__":
    unittest.main()
