from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data_agent.runtime import (
    AgentEventType,
    AgentRequest,
    AgentResponse,
    PrincipalContext,
    ProductRuntime,
    USER_DATASET_DOMAIN_ID,
    UploadDatasetRuntime,
)
from data_agent.runtime.models import AgentRow, ChartSpec
from data_agent.runtime.composition_root import build_upload_runtime
from data_agent.runtime.errors import ErrorCode


class _Model:
    model_id = "dataset-planner"
    version = "test-v1"

    def __init__(self) -> None:
        self.close_calls = 0

    async def complete(self, prompt: str, **kwargs) -> str:
        del prompt, kwargs
        return "{}"

    async def close(self) -> None:
        self.close_calls += 1


class UploadDatasetRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "conversations.sqlite3"
        )
        self.runtime = UploadDatasetRuntime(self.database_path)
        self.principal = PrincipalContext(
            tenant_id="tenant-a",
            user_id="user-a",
        )

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_runtime_never_falls_back_to_a_bundled_dataset(self) -> None:
        self.assertIsInstance(self.runtime, ProductRuntime)
        events = [
            event
            async for event in self.runtime.run(
                AgentRequest(
                    question="show revenue",
                    enterprise_id="user-dataset",
                    domain_id="dataset",
                ),
                self.principal,
            )
        ]

        self.assertEqual(
            [event.type for event in events],
            [AgentEventType.RUN_STARTED, AgentEventType.RUN_FAILED],
        )
        self.assertEqual(
            events[-1].response.error.code,
            ErrorCode.INVALID_REQUEST,
        )
        self.assertIn("上传数据集", events[-1].response.error.message)

    async def test_conversations_and_dataset_turns_survive_restart(self) -> None:
        conversation = await self.runtime.create_conversation(
            principal=self.principal,
            domain_id=USER_DATASET_DOMAIN_ID,
            title="Sales",
        )
        request = AgentRequest(
            question="top orders",
            enterprise_id="user-dataset",
            domain_id="dataset.orders",
            conversation_id=conversation.conversation_id,
            source_id="orders",
            source_version=1,
            binding_id="orders-binding",
            binding_version=1,
        )
        await self.runtime.record_conversation_turn(
            request=request,
            principal=self.principal,
            response=AgentResponse(
                ok=True,
                question=request.question,
                contextualized_question=request.question,
                conversation_id=conversation.conversation_id,
                tenant_id=self.principal.tenant_id,
                dataset_query_plan={
                    "status": "ready",
                    "analysis_type": "aggregate",
                    "select": [],
                    "aggregations": [
                        {
                            "ref": "dataset.Orders.total",
                            "operation": "sum",
                            "alias": "total_amount",
                        }
                    ],
                    "group_by": ["dataset.Orders.state"],
                    "filters": [],
                    "order_by": [],
                    "limit": 10,
                    "clarification_question": None,
                },
                sql="SELECT state, SUM(amount) AS total_amount FROM orders",
                message_type="chart",
                rows=(
                    AgentRow(root={"state": "SP", "total_amount": 30}),
                ),
                chart=ChartSpec(
                    title="Sales by state",
                    x_field="state",
                    y_field="total_amount",
                ),
                answer="A-1",
            ),
        )

        reopened = UploadDatasetRuntime(self.database_path)
        restored = await reopened.get_conversation(
            principal=self.principal,
            domain_id=USER_DATASET_DOMAIN_ID,
            conversation_id=conversation.conversation_id,
        )
        messages = await reopened.list_conversation_messages(
            principal=self.principal,
            domain_id=USER_DATASET_DOMAIN_ID,
            conversation_id=conversation.conversation_id,
            limit=10,
        )
        stranger = await reopened.get_conversation(
            principal=PrincipalContext(
                tenant_id="tenant-a",
                user_id="user-b",
            ),
            domain_id=USER_DATASET_DOMAIN_ID,
            conversation_id=conversation.conversation_id,
        )

        self.assertIsNotNone(restored)
        self.assertEqual(restored.conversation_id, conversation.conversation_id)
        self.assertEqual(restored.title, "Sales")
        self.assertEqual(
            [(item.role, item.content) for item in messages],
            [("user", "top orders"), ("assistant", "A-1")],
        )
        assistant = messages[1].metadata
        self.assertEqual(assistant.sql, "SELECT state, SUM(amount) AS total_amount FROM orders")
        self.assertEqual(
            assistant.rows[0].root,
            {"state": "SP", "total_amount": 30},
        )
        self.assertEqual(assistant.chart.x_field, "state")
        self.assertEqual(
            assistant.dataset_query_plan["analysis_type"],
            "aggregate",
        )
        self.assertIsNone(stranger)

    async def test_archived_rows_do_not_consume_the_active_list_limit(self) -> None:
        archived = await self.runtime.create_conversation(
            principal=self.principal,
            domain_id=USER_DATASET_DOMAIN_ID,
            title="Archived",
        )
        active = await self.runtime.create_conversation(
            principal=self.principal,
            domain_id=USER_DATASET_DOMAIN_ID,
            title="Active",
        )
        await self.runtime.update_conversation(
            principal=self.principal,
            domain_id=USER_DATASET_DOMAIN_ID,
            conversation_id=archived.conversation_id,
            archived=True,
        )

        conversations = await self.runtime.list_conversations(
            principal=self.principal,
            domain_id=USER_DATASET_DOMAIN_ID,
            limit=1,
        )

        self.assertEqual(
            [item.conversation_id for item in conversations],
            [active.conversation_id],
        )

    async def test_missing_datasource_failure_is_saved_for_a_conversation(self) -> None:
        conversation = await self.runtime.create_conversation(
            principal=self.principal,
            domain_id=USER_DATASET_DOMAIN_ID,
        )
        request = AgentRequest(
            question="show revenue",
            conversation_id=conversation.conversation_id,
        )

        _ = [
            event async for event in self.runtime.run(request, self.principal)
        ]
        messages = await self.runtime.list_conversation_messages(
            principal=self.principal,
            domain_id=USER_DATASET_DOMAIN_ID,
            conversation_id=conversation.conversation_id,
            limit=10,
        )

        self.assertEqual(
            [item.role for item in messages],
            ["user", "assistant"],
        )
        self.assertIn("上传数据集", messages[-1].content)

    async def test_default_composition_needs_no_olist_bundle_or_database(self) -> None:
        model = _Model()
        composition = await build_upload_runtime(
            state_root=self.temporary_directory.name,
            model_client_factory=lambda: model,
            environment={},
        )

        self.assertIsInstance(composition.runtime, UploadDatasetRuntime)
        self.assertIs(composition.dependencies.model_client, model)
        await composition.close()
        await composition.close()
        self.assertEqual(model.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
