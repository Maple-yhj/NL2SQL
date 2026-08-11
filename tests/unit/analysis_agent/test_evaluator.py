from __future__ import annotations

import unittest

from data_agent.analysis_agent.evaluator import AnalysisEvaluator
from data_agent.public_contracts import AgentError, ErrorCode

from ._decision_support import (
    SequenceModel,
    artifact,
    authority,
    evidence,
    observation,
    plan,
)


def evaluation_document(
    *,
    decision: str = "finish",
    sufficient: bool = True,
    missing: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "decision": decision,
        "evidence_sufficient": sufficient,
        "completed_step_ids": ["query"] if decision == "finish" else [],
        "missing_evidence": list(missing),
        "contradictions": [],
        "clarification": None,
        "rationale_summary": "Evidence satisfies the completion criteria.",
    }


class AnalysisEvaluatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_intermediate_tool_continues_without_another_model_call(self) -> None:
        model = SequenceModel([])
        current_plan = plan(status="completed").model_copy(
            update={
                "steps": (
                    plan(status="completed").steps[0],
                    plan(status="pending").steps[0].model_copy(
                        update={"step_id": "next_query"}
                    ),
                )
            }
        )
        result = await AnalysisEvaluator(model).evaluate(
            run_id="run-1",
            plan=current_plan,
            authority=authority(),
            observations=(
                observation(
                    artifact_refs=(artifact(kind="catalog", row_count=None),),
                    evidence_refs=(),
                    safe_preview=(),
                ),
            ),
            artifacts=(artifact(kind="catalog", row_count=None),),
            evidence=(),
        )

        self.assertEqual(result.decision, "continue")
        self.assertEqual(result.completed_step_ids, ("query",))
        self.assertEqual(model.calls, [])

    async def test_completed_query_without_evidence_continues_to_binding_without_model(self) -> None:
        model = SequenceModel([])
        evaluator = AnalysisEvaluator(model)
        completed_plan = plan(status="completed")
        result_artifact = artifact(kind="query_preview", row_count=8)
        result_observation = observation(
            tool_name="query.preview",
            artifact_refs=(result_artifact,),
            evidence_refs=(),
            safe_preview=({"order_status": "delivered", "order_count": 96478},),
        )
        kwargs = {
            "run_id": "run-1",
            "plan": completed_plan,
            "authority": authority(mode="preview"),
            "observations": (result_observation,),
            "artifacts": (result_artifact,),
            "evidence": (),
        }

        self.assertFalse(evaluator.requires_model_call(**kwargs))
        decision = await evaluator.evaluate(**kwargs)

        self.assertEqual(decision.decision, "continue")
        self.assertIn("evidence binding", decision.rationale_summary)
        self.assertEqual(model.calls, [])

    async def test_existing_semantic_evidence_does_not_block_deterministic_progress(self) -> None:
        model = SequenceModel([])
        current_plan = plan(status="completed").model_copy(
            update={
                "steps": (
                    plan(status="completed").steps[0],
                    plan(status="pending").steps[0].model_copy(
                        update={"step_id": "compile_query"}
                    ),
                )
            }
        )

        decision = await AnalysisEvaluator(model).evaluate(
            run_id="run-1",
            plan=current_plan,
            authority=authority(),
            observations=(observation(),),
            artifacts=(artifact(),),
            evidence=(evidence(),),
            required_evidence_keys=("total_revenue",),
        )

        self.assertEqual(decision.decision, "continue")
        self.assertEqual(model.calls, [])

    async def test_tool_failure_empty_result_and_mismatch_override_the_model(self) -> None:
        model = SequenceModel([])
        evaluator = AnalysisEvaluator(model)
        failed = observation(
            status="failed",
            summary="provider failed",
            artifact_refs=(),
            evidence_refs=(),
            safe_preview=(),
            error=AgentError(
                code=ErrorCode.INTERNAL_ERROR,
                message="safe failure",
                retryable=False,
            ),
        )
        failure = await evaluator.evaluate(
            run_id="run-1",
            plan=plan(),
            authority=authority(),
            observations=(failed,),
            artifacts=(),
            evidence=(),
            required_evidence_keys=("total_revenue",),
        )
        self.assertEqual(failure.decision, "fail")
        self.assertIn("tool_error", failure.contradictions[0])

        empty_artifact = artifact(row_count=0)
        empty = observation(
            artifact_refs=(empty_artifact,),
            evidence_refs=(),
            safe_preview=(),
        )
        empty_result = await evaluator.evaluate(
            run_id="run-1",
            plan=plan(),
            authority=authority(),
            observations=(empty,),
            artifacts=(empty_artifact,),
            evidence=(),
        )
        self.assertEqual(empty_result.decision, "replan")

        stale = artifact(schema_digest="sha256:" + "f" * 64)
        mismatch = await evaluator.evaluate(
            run_id="run-1",
            plan=plan(),
            authority=authority(),
            observations=(),
            artifacts=(stale,),
            evidence=(),
        )
        self.assertEqual(mismatch.decision, "fail")
        self.assertEqual(len(model.calls), 0)

    async def test_valid_evidence_can_finish_and_unknown_steps_cannot(self) -> None:
        invalid = evaluation_document()
        invalid["completed_step_ids"] = ["unknown"]
        model = SequenceModel([invalid, evaluation_document()])
        result = await AnalysisEvaluator(model).evaluate(
            run_id="run-1",
            plan=plan(),
            authority=authority(),
            observations=(),
            artifacts=(artifact(),),
            evidence=(evidence(),),
            required_evidence_keys=("total_revenue",),
        )
        self.assertEqual(result.decision, "finish")
        self.assertTrue(result.evidence_sufficient)
        self.assertEqual(len(model.calls), 2)

    async def test_model_cannot_finish_without_required_evidence(self) -> None:
        model = SequenceModel(
            [
                evaluation_document(),
                evaluation_document(
                    decision="continue",
                    sufficient=False,
                    missing=("total_revenue",),
                ),
            ]
        )
        result = await AnalysisEvaluator(model).evaluate(
            run_id="run-1",
            plan=plan(),
            authority=authority(),
            observations=(),
            artifacts=(),
            evidence=(),
            required_evidence_keys=("total_revenue",),
        )
        self.assertEqual(result.decision, "continue")
        self.assertFalse(result.evidence_sufficient)
        self.assertEqual(len(model.calls), 2)

    async def test_contradiction_and_budget_have_explicit_outcomes(self) -> None:
        evaluator = AnalysisEvaluator(SequenceModel([]))
        contradiction = await evaluator.evaluate(
            run_id="run-1",
            plan=plan(),
            authority=authority(),
            observations=(),
            artifacts=(),
            evidence=(),
            deterministic_contradictions=("metric totals disagree",),
        )
        exhausted = await evaluator.evaluate(
            run_id="run-1",
            plan=plan(),
            authority=authority(),
            observations=(),
            artifacts=(),
            evidence=(),
            budget_exhausted=True,
        )
        self.assertEqual(contradiction.decision, "replan")
        self.assertEqual(exhausted.decision, "fail")


if __name__ == "__main__":
    unittest.main()
