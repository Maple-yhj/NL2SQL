from __future__ import annotations

import asyncio
from types import SimpleNamespace

from data_agent.analysis_agent.composition import (
    _DatasetNextActionResolver,
    _dataset_plan_refs,
)
from data_agent.analysis_agent.models import AnalysisGoal
from data_agent.dataset_query import DatasetAggregation, DatasetOrdering, DatasetQueryPlan

from ._decision_support import artifact, authority, observation, plan


def _goal(question: str) -> AnalysisGoal:
    return AnalysisGoal(
        original_question=question,
        contextualized_question=question,
        requested_output="answer",
        success_criteria=("Return a governed query plan",),
    )


def test_plan_mode_completion_explicitly_says_that_data_was_not_read() -> None:
    chinese = _DatasetNextActionResolver._finish_plan_mode(
        plan(),
        goal=_goal("规划每个订单状态的订单数量"),
    )
    english = _DatasetNextActionResolver._finish_plan_mode(
        plan(),
        goal=_goal("Plan order counts by status"),
    )

    assert chinese.decision == "finish"
    assert "未执行或预览任何数据" in (chinese.completion_summary or "")
    assert "did not execute or preview any data" in (
        english.completion_summary or ""
    )
    assert all(step.status == "skipped" for step in chinese.plan.steps)


def test_dataset_plan_refs_exclude_aggregation_aliases_from_relationship_routing() -> None:
    query_plan = DatasetQueryPlan(
        analysis_type="aggregate",
        aggregations=(
            DatasetAggregation(
                ref="commerce.Items.price",
                operation="sum",
                alias="total_amount",
            ),
        ),
        group_by=("commerce.Products.category",),
        order_by=(DatasetOrdering(ref="total_amount", direction="desc"),),
    )

    assert _dataset_plan_refs(query_plan) == (
        "commerce.Items.price",
        "commerce.Products.category",
    )


def test_preview_result_appends_evidence_step_when_model_plan_omits_it() -> None:
    prepared = artifact(
        artifact_id="artifact-prepared",
        kind="prepared_query",
        row_count=None,
        sensitivity="metadata",
    )
    result = artifact(
        artifact_id="artifact-preview",
        kind="query_preview",
        row_count=8,
    )
    latest = observation(
        action_id="action-preview",
        tool_name="query.preview",
        artifact_refs=(result,),
        evidence_refs=(),
        safe_preview=({"order_status": "delivered", "order_count": 96478},),
    )
    resolver = _DatasetNextActionResolver(
        model_client=object(),
        binding=object(),
        catalog=object(),
    )

    decision = asyncio.run(
        resolver(
            state={
                "run_id": "run-preview",
                "plan": plan(status="completed"),
                "goal": _goal("预览每个订单状态的订单数量"),
                "authority": authority(mode="preview"),
                "observations": (latest,),
                "artifact_refs": (prepared, result),
                "evidence_refs": (),
            },
            allowed_tools=(SimpleNamespace(name="evidence.collect"),),
        )
    )

    assert decision is not None
    assert decision.decision == "act"
    assert decision.next_action is not None
    assert decision.next_action.tool_name == "evidence.collect"
    assert decision.plan.revision == 2
    assert decision.plan.steps[-1].step_id == "bind_evidence"
    assert decision.plan.steps[-1].status == "pending"
