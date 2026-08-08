from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from api.datasource_service import DataSourceService
from data_agent.analysis_agent.checkpoints import SQLiteCheckpointerFactory
from data_agent.analysis_agent.composition import (
    build_analysis_agent_runtime,
    build_analysis_runtime_from_resolver,
)
from data_agent.analysis_agent.graph import build_analysis_agent_graph
from data_agent.analysis_agent.models import (
    AgentAnswerDraft,
    AgentInputReason,
    AgentInputRequest,
    AgentRunBudget,
    EvaluationDecision,
    FindingDraft,
    PlannerDecision,
)
from data_agent.datasources import SemanticFieldMapping
from data_agent.public_contracts import ErrorCode
from data_agent.runtime.events import AgentEventType
from data_agent.runtime.models import AgentMode, AgentRequest, PrincipalContext
from data_agent.tools.providers.dataset import build_dataset_tool_registry
from tests.integration.test_analysis_agent_runtime import (
    PRINCIPAL,
    TestAnalysisResolver,
    analysis_request,
    clarification_decision,
)
from tests.support.analysis_agent_evaluator import (
    assert_evidence_and_pins,
    assert_read_only_sql,
    assert_trajectory_invariants,
)
from tests.support.analysis_agent_models import AgentTrajectoryModel
from tests.unit.analysis_agent._graph_support import (
    GroundedSynthesizer,
    SequenceEvaluator,
    SequencePlanner,
    SequenceToolExecutor,
    act_decision,
    analysis_plan,
    finish_decision,
    graph_context,
)
from tests.unit.analysis_agent.test_graph import graph_input


FIXTURE = Path(__file__).parents[1] / "fixtures" / "analysis_agent_cases.json"


class PreviewSynthesizer(GroundedSynthesizer):
    async def synthesize(self, **kwargs: object) -> AgentAnswerDraft:
        evidence = kwargs["evidence"]
        evidence_ids = tuple(item.evidence_id for item in evidence)
        return AgentAnswerDraft(
            answer="The preview result is supported by evidence.",
            key_findings=(
                FindingDraft(
                    finding_id="preview-finding",
                    claim="The preview result is supported.",
                    evidence_ids=evidence_ids,
                ),
            ),
            limitations=("Based on a bounded preview, not a full execution.",),
            evidence_ids=evidence_ids,
        )


class AnalysisAgentTrajectoryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.cases = {item["id"]: item for item in document["cases"]}

    async def test_single_aggregate_matches_result_oracle_with_no_redundant_calls(self) -> None:
        case = self.cases["single_aggregate"]
        with tempfile.TemporaryDirectory() as directory:
            service = DataSourceService(state_root=directory)
            try:
                await service.import_file_source(
                    tenant_id="tenant-a",
                    source_id="orders",
                    name="Orders",
                    uploads=[
                        UploadFile(
                            filename="orders.csv",
                            file=BytesIO(b"order_id,amount\nA-1,5\nA-2,20\nA-3,10\n"),
                        )
                    ],
                )
                draft = await service.create_binding(
                    tenant_id="tenant-a",
                    source_id="orders",
                    binding_id="orders-binding",
                    domain_id="dataset.orders",
                    mappings=(
                        SemanticFieldMapping(
                            logical_ref="dataset.Orders.order_id",
                            physical_relation="public.orders",
                            physical_column="order_id",
                        ),
                        SemanticFieldMapping(
                            logical_ref="dataset.Orders.total",
                            physical_relation="public.orders",
                            physical_column="amount",
                        ),
                    ),
                )
                binding = await service.activate_binding(
                    tenant_id="tenant-a",
                    source_id="orders",
                    binding_id=draft.binding_id,
                )
                principal = PrincipalContext(tenant_id="tenant-a", user_id="analyst-a")
                request = AgentRequest(
                    question="sum all order amounts",
                    conversation_id="trajectory-conversation",
                    source_id="orders",
                    source_version=1,
                    binding_id=binding.binding_id,
                    binding_version=binding.version,
                    domain_id=binding.domain_id,
                    mode=AgentMode.EXECUTE,
                )
                composition = await build_analysis_agent_runtime(
                    data_sources=service,
                    model_client=AgentTrajectoryModel(),
                    state_root=directory,
                )
                try:
                    events = [
                        item
                        async for item in composition.runtime.run(
                            request, principal, run_id="run-oracle-aggregate"
                        )
                    ]
                    state = await composition.runtime.state(
                        "run-oracle-aggregate", principal=principal
                    )
                finally:
                    await composition.close()
                response = events[-1].response
                self.assertEqual(response.rows[0].root["total_amount"], case["oracle_value"])
                self.assertIn(str(case["oracle_value"]), response.answer)
                assert_evidence_and_pins(self, response)
                assert_read_only_sql(
                    self, response.sql or "", allowed_relations={"public.orders"}
                )
                assert_trajectory_invariants(
                    self,
                    state,
                    allowed_tools=set(build_dataset_tool_registry().names()),
                    minimum_tool_calls=case["minimum_tool_calls"],
                    maximum_tool_calls=case["maximum_tool_calls"],
                )
            finally:
                await service.close()

    async def test_real_graph_multi_query_trend_anomaly_and_empty_replan_trajectories(self) -> None:
        trajectories = (
            ("multi_query_comparison", ["query.preview", "query.preview"], ["success", "success"]),
            ("trend_anomaly", ["query.preview", "data.profile", "analysis.compute", "chart.render", "evidence.collect"], ["success"] * 5),
            ("empty_result_replan", ["query.preview", "query.preview"], ["empty", "success"]),
        )
        allowed = set(build_dataset_tool_registry().names())
        for case_id, tool_names, outcomes in trajectories:
            with self.subTest(case=case_id):
                decisions = []
                evaluations = []
                for index, tool_name in enumerate(tool_names):
                    decision = act_decision(
                        action_id=f"{case_id}-{index}",
                        plan_value=analysis_plan("pending", revision=index + 1),
                        tool_name=tool_name,
                    )
                    if tool_name == "analysis.compute":
                        decision = decision.model_copy(
                            update={
                                "next_action": decision.next_action.model_copy(
                                    update={
                                        "arguments": {
                                            "operation": "outlier_iqr",
                                            "artifact_id": "artifact-" + "a" * 64,
                                            "fields": ["amount"],
                                        }
                                    }
                                )
                            }
                        )
                    elif tool_name == "chart.render":
                        decision = decision.model_copy(
                            update={
                                "next_action": decision.next_action.model_copy(
                                    update={
                                        "arguments": {
                                            "artifact_id": "artifact-" + "a" * 64,
                                            "title": "Trend and anomalies",
                                            "x_field": "period",
                                            "y_field": "amount",
                                        }
                                    }
                                )
                            }
                        )
                    elif tool_name == "evidence.collect":
                        decision = decision.model_copy(
                            update={
                                "next_action": decision.next_action.model_copy(
                                    update={
                                        "arguments": {
                                            "artifact_id": "artifact-" + "a" * 64,
                                            "claim_key": "trend_anomaly",
                                            "field_refs": ["period", "amount"],
                                        }
                                    }
                                )
                            }
                        )
                    decisions.append(decision)
                    if index < len(tool_names) - 1:
                        evaluations.append(
                            EvaluationDecision(
                                decision="replan" if outcomes[index] == "empty" else "continue",
                                evidence_sufficient=False,
                                completed_step_ids=(),
                                missing_evidence=("next evidence",),
                                contradictions=(),
                                rationale_summary="Continue the bounded trajectory.",
                            )
                        )
                evaluations.append(
                    EvaluationDecision(
                        decision="finish",
                        evidence_sufficient=True,
                        completed_step_ids=("step-1",),
                        missing_evidence=(),
                        contradictions=(),
                        rationale_summary="Evidence is sufficient.",
                    )
                )
                trajectory_limits = AgentRunBudget(max_model_calls=16)
                output = await build_analysis_agent_graph(
                    budget_limits=trajectory_limits
                ).ainvoke(
                    graph_input(AgentMode.PREVIEW, run_id=f"run-{case_id}"),
                    context=graph_context(
                        mode=AgentMode.PREVIEW,
                        planner=SequencePlanner(decisions),
                        evaluator=SequenceEvaluator(evaluations),
                        tools=SequenceToolExecutor(list(outcomes)),
                        budget_limits=trajectory_limits,
                    ),
                )
                case = self.cases[case_id]
                assert_trajectory_invariants(
                    self,
                    output,
                    allowed_tools=allowed,
                    minimum_tool_calls=case["minimum_tool_calls"],
                    maximum_tool_calls=case["maximum_tool_calls"],
                )
                self.assertTrue(output["final_response"].ok)

    async def test_ambiguity_preview_budget_and_cancellation_close_safely(self) -> None:
        for reason, case_id in (
            (AgentInputReason.CLARIFICATION, "schema_ambiguity"),
            (AgentInputReason.CONFLICT_RESOLUTION, "relationship_ambiguity"),
        ):
            decision = PlannerDecision(
                plan=analysis_plan("pending"),
                decision="clarify",
                clarification=AgentInputRequest(
                    interrupt_id=f"interrupt-{case_id}",
                    reason=reason,
                    prompt="Choose the intended field or relationship path.",
                    choices=("Option A", "Option B"),
                ),
                rationale_summary="The schema is ambiguous.",
            )
            graph = build_analysis_agent_graph(checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": f"run-{case_id}"}}
            context = graph_context(
                mode=AgentMode.PLAN,
                planner=SequencePlanner([decision, finish_decision(analysis_plan("pending"))]),
            )
            interrupted = await graph.ainvoke(
                graph_input(AgentMode.PLAN, run_id=f"run-{case_id}"),
                context=context,
                config=config,
            )
            self.assertIn("__interrupt__", interrupted)
            resumed = await graph.ainvoke(
                Command(resume={"message": "Option A", "selected_choice": "Option A"}),
                context=context,
                config=config,
            )
            self.assertTrue(resumed["final_response"].ok)

        preview = await build_analysis_agent_graph().ainvoke(
            graph_input(AgentMode.PREVIEW, run_id="run-preview-limit"),
            context=graph_context(
                mode=AgentMode.PREVIEW,
                planner=SequencePlanner([
                    act_decision(action_id="preview", plan_value=analysis_plan("pending"))
                ]),
                synthesizer=PreviewSynthesizer(),
            ),
        )
        self.assertTrue(any("preview" in item.casefold() for item in preview["final_response"].limitations))

        limits = AgentRunBudget(max_agent_steps=1, max_model_calls=5)
        tools = SequenceToolExecutor(["success", "success"])
        budgeted = await build_analysis_agent_graph(budget_limits=limits).ainvoke(
            graph_input(AgentMode.PREVIEW, run_id="run-budget-gate"),
            context=graph_context(
                mode=AgentMode.PREVIEW,
                planner=SequencePlanner([
                    act_decision(action_id="one", plan_value=analysis_plan("pending")),
                    act_decision(action_id="two", plan_value=analysis_plan("pending", revision=2)),
                ]),
                evaluator=SequenceEvaluator([
                    EvaluationDecision(
                        decision="continue", evidence_sufficient=False,
                        completed_step_ids=(), missing_evidence=("more",),
                        contradictions=(), rationale_summary="Continue.",
                    )
                ]),
                tools=tools,
                budget_limits=limits,
            ),
        )
        self.assertEqual(budgeted["final_response"].error.code, ErrorCode.AGENT_MAX_STEPS_EXCEEDED)
        self.assertEqual(len(tools.calls), 1)

        cancelled_tools = SequenceToolExecutor(["success"])
        cancelled = await build_analysis_agent_graph().ainvoke(
            graph_input(AgentMode.PREVIEW, run_id="run-cancel-gate"),
            context=graph_context(
                mode=AgentMode.PREVIEW,
                planner=SequencePlanner([]),
                tools=cancelled_tools,
                cancelled=lambda: True,
            ),
        )
        self.assertEqual(str(cancelled["status"]), "cancelled")
        self.assertEqual(cancelled_tools.calls, [])

    async def test_sqlite_restart_resume_is_a_single_monotonic_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = await build_analysis_runtime_from_resolver(
                resolver=TestAnalysisResolver([clarification_decision()]),
                checkpointer_factory=SQLiteCheckpointerFactory(directory),
            )
            waiting = [
                event
                async for event in first.runtime.run(
                    analysis_request(), PRINCIPAL, run_id="run-trajectory-restart"
                )
            ]
            await first.close()
            self.assertEqual(waiting[-1].type, AgentEventType.RUN_WAITING)

            second = await build_analysis_runtime_from_resolver(
                resolver=TestAnalysisResolver([finish_decision(analysis_plan("pending"))]),
                checkpointer_factory=SQLiteCheckpointerFactory(directory),
            )
            try:
                resumed = [
                    event
                    async for event in second.runtime.resume(
                        run_id="run-trajectory-restart",
                        response={
                            "interrupt_id": "interrupt-time-range",
                            "message": "Use the last 12 months",
                        },
                        principal=PRINCIPAL,
                        start_sequence=waiting[-1].sequence + 1,
                    )
                ]
            finally:
                await second.close()
            self.assertEqual(resumed[0].type, AgentEventType.RUN_RESUMED)
            self.assertEqual(resumed[-1].type, AgentEventType.RUN_COMPLETED)
            all_sequences = [item.sequence for item in waiting + resumed]
            self.assertEqual(all_sequences, list(range(len(all_sequences))))


if __name__ == "__main__":
    unittest.main()
