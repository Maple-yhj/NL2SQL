from __future__ import annotations

import json
import unittest

from data_agent.analysis_agent.planner import AnalysisPlanner
from data_agent.tools.providers.dataset import build_dataset_tool_registry

from ._decision_support import SequenceModel, context, goal, plan


def planner_document(*, tool_name: str = "catalog.inspect") -> dict[str, object]:
    return {
        "plan": plan().model_dump(mode="json"),
        "decision": "act",
        "next_action": {
            "action_id": "inspect-catalog",
            "tool_name": tool_name,
            "arguments": {},
            "purpose": "Inspect safe catalog metadata",
            "expected_evidence": ["catalog metadata"],
        },
        "clarification": None,
        "completion_summary": None,
        "rationale_summary": "Catalog context is needed before querying.",
    }


class AnalysisPlannerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        registry = build_dataset_tool_registry()
        self.allowed = tuple(
            spec
            for spec in registry.specs()
            if spec.name in {"catalog.inspect", "query.compile"}
        )

    async def test_planner_uses_only_bounded_public_inputs_and_strict_schema(self) -> None:
        model = SequenceModel([planner_document()])
        decision = await AnalysisPlanner(model).decide(
            goal=goal(),
            context=context(),
            current_plan=None,
            observations=(),
            budget_remaining={"model_calls": 4, "tool_calls": 8},
            allowed_tools=self.allowed,
        )
        self.assertEqual(decision.next_action.tool_name, "catalog.inspect")
        request = json.loads(model.calls[0]["prompt"])
        self.assertEqual(request["task"], "plan_or_replan_analysis")
        self.assertEqual(
            {item["name"] for item in request["trustedData"]["allowedTools"]},
            {"catalog.inspect", "query.compile"},
        )
        self.assertNotIn("authority", request["untrustedData"])
        self.assertNotIn("credential_requirement", model.calls[0]["prompt"])
        self.assertNotIn("provider", model.calls[0]["prompt"].casefold())
        self.assertNotIn("chain-of-thought", decision.rationale_summary.casefold())

    async def test_invalid_json_unknown_tool_and_invalid_plan_use_bounded_correction(self) -> None:
        invalid_cycle = planner_document()
        invalid_cycle["plan"] = {
            "plan_id": "analysis-plan",
            "revision": 1,
            "steps": [
                {
                    "step_id": "one",
                    "objective": "one",
                    "status": "pending",
                    "depends_on": ["two"],
                    "expected_evidence": [],
                },
                {
                    "step_id": "two",
                    "objective": "two",
                    "status": "pending",
                    "depends_on": ["one"],
                    "expected_evidence": [],
                },
            ],
            "completion_criteria": ["done"],
        }
        for first in ("```json\n{}\n```", planner_document(tool_name="shell.exec"), invalid_cycle):
            model = SequenceModel([first, planner_document()])
            decision = await AnalysisPlanner(model).decide(
                goal=goal(),
                context=context(),
                current_plan=None,
                observations=(),
                budget_remaining={"model_calls": 3},
                allowed_tools=self.allowed,
            )
            self.assertEqual(decision.decision, "act")
            self.assertEqual(len(model.calls), 2)
            correction = json.loads(model.calls[1]["prompt"])
            self.assertTrue(correction["task"].startswith("repair_"))
            self.assertIn("validationErrors", correction["trustedData"])

    async def test_invalid_outputs_fail_after_finite_attempts(self) -> None:
        model = SequenceModel(["not-json", "also-not-json"])
        with self.assertRaisesRegex(ValueError, "after 2 attempt"):
            await AnalysisPlanner(model, max_attempts=2).decide(
                goal=goal(),
                context=context(),
                current_plan=None,
                observations=(),
                budget_remaining={"model_calls": 2},
                allowed_tools=self.allowed,
            )
        self.assertEqual(len(model.calls), 2)

    async def test_follow_up_goal_uses_summary_not_a_prior_temporary_action(self) -> None:
        rebuilt = AnalysisPlanner.rebuild_follow_up_goal(
            question="How about last month?",
            context=context(conversation_summary="Prior answer covered this quarter."),
        )
        self.assertEqual(rebuilt.original_question, "How about last month?")
        self.assertIn("Prior answer covered this quarter", rebuilt.contextualized_question)
        self.assertNotIn("pending_action", rebuilt.contextualized_question)


if __name__ == "__main__":
    unittest.main()
