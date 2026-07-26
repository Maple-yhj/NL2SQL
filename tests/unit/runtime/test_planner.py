from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from data_agent.execution import ResolvedContext
from data_agent.runtime import ModelLogicalPlanner
from data_agent.tools.providers import SemanticMatch


VALID_PLAN = {
    "analysisType": "metric",
    "metrics": ["commerce.order_count"],
    "entities": ["commerce.OrderItem"],
    "requestedEvidence": ["semantic_resolution", "query_result"],
    "resultShape": "scalar",
}


class _SequenceModelClient:
    model_id = "planner-test"
    version = "test-v1"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, int]] = []

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str:
        self.calls.append((prompt, system, max_output_tokens))
        return self.responses.pop(0)


def _context():
    return SimpleNamespace(
        mode=SimpleNamespace(value="execute"),
        bundle=SimpleNamespace(
            semantic_model={
                "entities": {
                    "commerce.OrderItem": {
                        "description": "每个订单商品项一条记录",
                        "grain": ["order_id", "item_sequence"],
                        "fields": {
                            "order_id": {
                                "type": "string",
                                "description": "所属订单标识",
                            }
                        },
                    }
                },
                "metrics": {
                    "commerce.order_count": {
                        "aggregation": "count_distinct",
                        "inputs": ["commerce.OrderItem.order_id"],
                        "description": "去重订单数量",
                    }
                },
                "relationships": [],
                "policies": [],
                "evals": [{"question": "must not be sent"}],
            }
        ),
    )


def _resolved_context():
    return ResolvedContext(
        contextualized_question="OList 数据集中共有多少个不同的订单？"
    )


def _semantic_matches():
    return (
        SemanticMatch(
            ref="commerce.order_count",
            kind="metric",
            label="order_count",
            description="去重订单数量",
            score=0.9,
        ),
    )


class ModelLogicalPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_plan_uses_one_model_call_and_compact_catalog(self) -> None:
        client = _SequenceModelClient([json.dumps(VALID_PLAN)])
        planner = ModelLogicalPlanner(client)

        plan = await planner.build_plan(
            context=_context(),
            resolved_context=_resolved_context(),
            semantic_matches=_semantic_matches(),
        )

        self.assertEqual(plan.metrics, ("commerce.order_count",))
        self.assertEqual(len(client.calls), 1)
        prompt, system, max_tokens = client.calls[0]
        self.assertIn('"canonicalSemanticCatalog"', prompt)
        self.assertIn('"commerce.order_count"', prompt)
        self.assertNotIn("must not be sent", prompt)
        self.assertIn("Do not echo the input", system)
        self.assertEqual(max_tokens, 4096)

    async def test_echoed_request_is_repaired_once(self) -> None:
        echoed_request = json.dumps(
            {
                "question": "OList 数据集中共有多少个不同的订单？",
                "canonicalSemanticMatches": [],
                "logicalPlanSchema": {},
            }
        )
        client = _SequenceModelClient(
            [echoed_request, f"```json\n{json.dumps(VALID_PLAN)}\n```"]
        )
        planner = ModelLogicalPlanner(client)

        plan = await planner.build_plan(
            context=_context(),
            resolved_context=_resolved_context(),
            semantic_matches=(),
        )

        self.assertEqual(plan.result_shape, "scalar")
        self.assertEqual(len(client.calls), 2)
        repair_prompt = client.calls[1][0]
        self.assertIn('"task":"repair_logical_query_plan"', repair_prompt)
        self.assertIn('"validationErrors"', repair_prompt)
        self.assertIn("analysisType", repair_prompt)
        self.assertIn("resultShape", repair_prompt)

    async def test_invalid_responses_stop_at_configured_attempt_limit(self) -> None:
        client = _SequenceModelClient(["{}", "{}"])
        planner = ModelLogicalPlanner(client, max_attempts=2)

        with self.assertRaisesRegex(
            ValueError,
            "failed to produce a valid LogicalQueryPlan after 2 attempt",
        ):
            await planner.build_plan(
                context=_context(),
                resolved_context=_resolved_context(),
                semantic_matches=(),
            )

        self.assertEqual(len(client.calls), 2)

    def test_attempt_limit_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 3"):
            ModelLogicalPlanner(_SequenceModelClient([]), max_attempts=4)


if __name__ == "__main__":
    unittest.main()
