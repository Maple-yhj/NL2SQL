from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from data_agent.analysis_agent.composition import (
    _DatasetNextActionResolver,
    _dataset_plan_refs,
)
from data_agent.analysis_agent.models import AnalysisGoal
from data_agent.analysis_agent.models import ClarificationTurn
from data_agent.dataset_query import (
    DatasetAggregateExpression,
    DatasetAggregation,
    DatasetFieldExpression,
    DatasetLiteralExpression,
    DatasetOrdering,
    DatasetProjection,
    DatasetQueryPlan,
    DatasetQueryProgram,
    DatasetQueryStage,
    DatasetRootSource,
)
from data_agent.semantic_metrics import (
    MetricAggregateFormula,
    MetricFieldExpression,
    MetricProposal,
    MetricProposalCandidate,
    SemanticMetricDefinitionV2,
)

from tests.unit.tools.dataset._support import DatasetToolHarness

from ._decision_support import (
    SequenceModel,
    artifact,
    authority,
    evidence,
    observation,
    plan,
)


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


def test_dataset_run_starts_with_a_deterministic_catalog_step() -> None:
    resolver = _DatasetNextActionResolver(
        model_client=object(),
        binding=object(),
        catalog=object(),
    )

    decision = resolver.initial_decision(
        state={"run_id": "run-initial"},
        allowed_tools=(SimpleNamespace(name="catalog.inspect"),),
    )

    assert decision.decision == "act"
    assert decision.next_action is not None
    assert decision.next_action.tool_name == "catalog.inspect"
    assert decision.plan.steps[0].objective == "Inspect the pinned dataset catalog"


def test_unknown_gmv_creates_and_reuses_agent_governance_draft() -> None:
    calls: list[str] = []
    proposal = MetricProposal(
        proposal_id="proposal-agent-gmv",
        tenant_id="tenant-1",
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint="sha256:olist",
        domain_id="commerce",
        base_binding_id="olist-binding",
        base_binding_version=1,
        requested_term="GMV",
        created_by="analyst",
        candidates=(
            MetricProposalCandidate(
                candidate_id="gmv-price",
                label="商品价格 GMV",
                rationale="汇总商品价格",
                required_decisions=("季度时间字段", "退款处理"),
                definition=SemanticMetricDefinitionV2(
                    metric_ref="commerce.gmv",
                    display_name="GMV",
                    description="Gross merchandise value",
                    formula=MetricAggregateFormula(
                        operation="sum",
                        operand=MetricFieldExpression(ref="item.price"),
                    ),
                ),
            ),
        ),
    )

    async def discover(term: str) -> MetricProposal:
        calls.append(term)
        return proposal

    resolver = _DatasetNextActionResolver(
        model_client=object(),
        binding=object(),
        catalog=object(),
        domain_id="commerce",
        metric_proposal_discovery=discover,
    )

    first = asyncio.run(resolver._discover_unresolved_metric("查询季度 GMV"))
    second = asyncio.run(resolver._discover_unresolved_metric("查询季度 GMV"))

    assert first == proposal
    assert second == proposal
    assert calls == ["GMV"]


def test_dataset_query_planning_uses_the_current_question_not_conversation_output() -> None:
    harness = DatasetToolHarness()
    try:
        model = SequenceModel(
            [
                DatasetQueryProgram(
                    status="needs_clarification",
                    clarification_question="Which governed amount should be used?",
                ).model_dump(mode="json")
            ]
        )
        resolver = _DatasetNextActionResolver(
            model_client=model,
            binding=harness.binding,
            catalog=harness.catalog,
        )
        goal = AnalysisGoal(
            original_question="Count the selected records",
            contextualized_question=(
                "Conversation summary: a large prior answer and diagnostics\n"
                "Current question: Count the selected records"
            ),
            requested_output="answer",
            success_criteria=("Return a governed query plan",),
        )

        asyncio.run(resolver._plan_query(goal))

        request = json.loads(str(model.calls[0]["prompt"]))
        assert request["question"] == "Count the selected records"
        assert "large prior answer" not in model.calls[0]["prompt"]
    finally:
        harness.close()


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


def test_metadata_question_collects_semantic_evidence_without_forcing_a_query() -> None:
    semantic = artifact(
        artifact_id="artifact-semantic",
        kind="logical_plan",
        row_count=None,
        sensitivity="metadata",
    )
    inspected = observation(
        action_id="action-semantic",
        tool_name="semantic.inspect",
        artifact_refs=(semantic,),
        evidence_refs=(),
        safe_preview=({"logical_ref": "dataset.orders.state"},),
    )
    harness = DatasetToolHarness()
    try:
        resolver = _DatasetNextActionResolver(
            model_client=object(),
            binding=harness.binding,
            catalog=harness.catalog,
        )
        decision = asyncio.run(
            resolver(
                state={
                    "run_id": "run-semantic-metadata",
                    "plan": plan(status="completed"),
                    "goal": _goal("What is the meaning of the state field?"),
                    "authority": authority(mode="execute"),
                    "observations": (inspected,),
                    "artifact_refs": (semantic,),
                    "evidence_refs": (),
                    "clarification_turns": (),
                },
                allowed_tools=(SimpleNamespace(name="evidence.collect"),),
            )
        )

        assert decision is not None
        assert decision.decision == "act"
        assert decision.next_action is not None
        assert decision.next_action.tool_name == "evidence.collect"
        assert decision.next_action.arguments["artifact_id"] == "artifact-semantic"
        assert decision.next_action.arguments["claim_key"] == "semantic_definition"
    finally:
        harness.close()


def test_result_evidence_is_collected_even_when_semantic_evidence_already_exists() -> None:
    semantic = artifact(
        artifact_id="artifact-semantic",
        kind="logical_plan",
        row_count=None,
        sensitivity="metadata",
    )
    prepared = artifact(
        artifact_id="artifact-prepared",
        kind="prepared_query",
        row_count=None,
        sensitivity="metadata",
    )
    result = artifact(artifact_id="artifact-result", kind="query_result", row_count=1)
    semantic_evidence = evidence(
        evidence_id="evidence-semantic",
        claim_key="semantic_definition",
        artifact_id=semantic.artifact_id,
        result_digest=semantic.digest,
        field_refs=("dataset.orders.customer_id",),
    )
    resolver = _DatasetNextActionResolver(
        model_client=object(),
        binding=object(),
        catalog=object(),
    )
    resolver._query_plan = DatasetQueryProgram(
        stages=(
            DatasetQueryStage(
                stage_id="summary",
                input=DatasetRootSource(anchor_ref="dataset.orders.customer_id"),
                projections=tuple(
                    DatasetProjection(
                        alias=alias,
                        expression=DatasetLiteralExpression(value=index),
                    )
                    for index, alias in enumerate(
                        (
                            "multi_product_orders",
                            "multi_seller_orders",
                            "max_product_count",
                            "max_seller_count",
                        ),
                        start=1,
                    )
                ),
            ),
        ),
        output_stage_id="summary",
    )
    latest = observation(
        action_id="action-query",
        tool_name="query.execute",
        artifact_refs=(result,),
        evidence_refs=(),
        safe_preview=(
            {
                "multi_product_orders": 3236,
                "multi_seller_orders": 1278,
                "max_product_count": 8,
                "max_seller_count": 5,
            },
        ),
    )

    decision = asyncio.run(
        resolver(
            state={
                "run_id": "run-result-after-semantic",
                "plan": plan(),
                "goal": _goal("Return all four order summary values"),
                "authority": authority(),
                "observations": (latest,),
                "artifact_refs": (semantic, prepared, result),
                "evidence_refs": (semantic_evidence,),
                "clarification_turns": (),
            },
            allowed_tools=(SimpleNamespace(name="evidence.collect"),),
        )
    )

    assert decision is not None and decision.next_action is not None
    assert decision.next_action.tool_name == "evidence.collect"
    assert decision.next_action.arguments["artifact_id"] == result.artifact_id
    assert decision.next_action.arguments["claim_key"] == "analysis_result"
    assert decision.next_action.arguments["field_refs"] == [
        "multi_product_orders",
        "multi_seller_orders",
        "max_product_count",
        "max_seller_count",
    ]


def test_field_lifecycle_question_finishes_from_semantic_evidence() -> None:
    semantic = artifact(
        artifact_id="artifact-lifecycle",
        kind="logical_plan",
        row_count=None,
        sensitivity="metadata",
    )
    semantic_evidence = evidence(
        evidence_id="evidence-lifecycle",
        claim_key="semantic_definition",
        artifact_id=semantic.artifact_id,
        result_digest=semantic.digest,
        field_refs=("dataset.orders.created_at",),
    )
    inspected = observation(
        action_id="action-lifecycle",
        tool_name="semantic.inspect",
        artifact_refs=(semantic,),
        evidence_refs=(),
        safe_preview=({"fields": []},),
    )
    resolver = _DatasetNextActionResolver(
        model_client=object(),
        binding=object(),
        catalog=object(),
    )

    decision = asyncio.run(
        resolver(
            state={
                "run_id": "run-lifecycle",
                "plan": plan(),
                "goal": _goal("预测时哪些字段会造成数据泄漏？"),
                "authority": authority(),
                "observations": (inspected,),
                "artifact_refs": (semantic,),
                "evidence_refs": (semantic_evidence,),
                "clarification_turns": (),
            },
            allowed_tools=(SimpleNamespace(name="semantic.inspect"),),
        )
    )

    assert decision is not None
    assert decision.decision == "finish"
    assert all(step.status in {"completed", "skipped"} for step in decision.plan.steps)


def test_dataset_query_clarification_resumes_from_structured_history() -> None:
    harness = DatasetToolHarness()
    try:
        needs_definition = DatasetQueryProgram(
            status="needs_clarification",
            clarification_question="Which amount definition should be used?",
        )
        ready = DatasetQueryProgram(
            stages=(
                DatasetQueryStage(
                    stage_id="summary",
                    input=DatasetRootSource(),
                    projections=(
                        DatasetProjection(
                            alias="amount_total",
                            expression=DatasetAggregateExpression(
                                operation="sum",
                                operand=DatasetFieldExpression(
                                    ref="dataset.orders.amount"
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            output_stage_id="summary",
        )
        resolver = _DatasetNextActionResolver(
            model_client=SequenceModel(
                [
                    needs_definition.model_dump(mode="json"),
                    ready.model_dump(mode="json"),
                ]
            ),
            binding=harness.binding,
            catalog=harness.catalog,
        )
        base_state = {
            "run_id": "run-structured-clarification",
            "plan": plan(),
            "goal": _goal("Compute the requested amount metric"),
            "authority": authority(mode="execute"),
            "observations": (observation(artifact_refs=(), evidence_refs=()),),
            "artifact_refs": (),
            "evidence_refs": (),
            "clarification_turns": (),
        }
        first = asyncio.run(
            resolver(
                state=base_state,
                allowed_tools=(SimpleNamespace(name="query.compile"),),
            )
        )
        assert first is not None
        assert first.decision == "clarify"
        assert first.clarification is not None
        assert first.clarification.origin == "dataset_query"

        turn = ClarificationTurn(
            request_fingerprint="a" * 64,
            interrupt_id=first.clarification.interrupt_id,
            reason=first.clarification.reason,
            origin="dataset_query",
            prompt=first.clarification.prompt,
            response="Use the governed amount field",
        )
        resumed_state = {
            **base_state,
            "replan_requested": True,
            "clarification_turns": (turn,),
        }
        assert resolver.requires_model_call(state=resumed_state)
        second = asyncio.run(
            resolver(
                state=resumed_state,
                allowed_tools=(SimpleNamespace(name="query.compile"),),
            )
        )

        assert second is not None
        assert second.decision == "act"
        assert second.next_action is not None
        assert second.next_action.tool_name == "query.compile"
        assert second.next_action.arguments["plan"]["schema_version"] == 2
    finally:
        harness.close()
