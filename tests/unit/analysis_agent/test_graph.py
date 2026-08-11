from __future__ import annotations

import asyncio
import time
import unittest
from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from data_agent.analysis_agent.graph import (
    ANALYSIS_GRAPH_DIGEST,
    ANALYSIS_GRAPH_ID,
    ANALYSIS_GRAPH_VERSION,
    analysis_graph_recursion_limit,
    build_analysis_agent_graph,
)
from data_agent.analysis_agent.models import (
    AgentInputRequest,
    AgentInputReason,
    AgentRunBudget,
    EvaluationDecision,
    PlannerDecision,
)
from data_agent.public_contracts import ErrorCode
from data_agent.runtime.events import AgentEvent, AgentEventType
from data_agent.runtime.models import AgentMode, AgentRequest

from ._decision_support import authority
from ._graph_support import (
    GroundedSynthesizer,
    SequenceEvaluator,
    SequencePlanner,
    SequenceToolExecutor,
    act_decision,
    analysis_plan,
    finish_decision,
    graph_context,
)


def graph_input(mode: AgentMode, *, run_id: str = "run-graph") -> dict[str, object]:
    auth = authority(mode.value)
    return {
        "run_id": run_id,
        "request": AgentRequest(
            question="Analyze the selected dataset",
            source_id=auth.source_id,
            source_version=auth.source_version,
            binding_id=auth.binding_id,
            binding_version=auth.binding_version,
            mode=mode,
        ),
        "authority": auth,
    }


class NativeAnalysisGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_stream_never_exposes_internal_chunks_and_has_one_terminal(self) -> None:
        planner = SequencePlanner(
            [act_decision(action_id="stream-action", plan_value=analysis_plan("pending"))]
        )
        events = [
            event
            async for event in build_analysis_agent_graph().astream_events(
                graph_input(AgentMode.PREVIEW, run_id="run-public-stream"),
                run_id="run-public-stream",
                context=graph_context(
                    mode=AgentMode.PREVIEW,
                    planner=planner,
                    tools=SequenceToolExecutor(["success"]),
                ),
            )
        ]
        self.assertTrue(all(isinstance(event, AgentEvent) for event in events))
        self.assertEqual([event.sequence for event in events], list(range(len(events))))
        terminals = [event for event in events if event.response is not None]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].type, AgentEventType.RUN_COMPLETED)
        self.assertEqual(events[0].type, AgentEventType.RUN_STARTED)

    async def test_one_action_completes_with_one_terminal_response_and_version_pins(self) -> None:
        planner = SequencePlanner(
            [act_decision(action_id="action-1", plan_value=analysis_plan("pending"))]
        )
        tools = SequenceToolExecutor(["success"])
        synthesizer = GroundedSynthesizer()
        runtime_context = graph_context(
            mode=AgentMode.PREVIEW,
            planner=planner,
            tools=tools,
            synthesizer=synthesizer,
        )
        graph = build_analysis_agent_graph()
        output = await graph.ainvoke(
            graph_input(AgentMode.PREVIEW),
            context=runtime_context,
        )

        self.assertEqual(output["status"].value, "completed")
        self.assertTrue(output["final_response"].ok)
        self.assertEqual(output["budget"].tool_calls, 1)
        self.assertEqual(output["final_response"].version_pins.graph_id, ANALYSIS_GRAPH_ID)
        self.assertEqual(output["final_response"].version_pins.graph_version, ANALYSIS_GRAPH_VERSION)
        self.assertEqual(output["final_response"].version_pins.graph_digest, ANALYSIS_GRAPH_DIGEST)
        self.assertEqual(len(synthesizer.calls), 1)

    async def test_two_distinct_tools_follow_a_real_dynamic_loop(self) -> None:
        first_plan = analysis_plan("pending", "pending")
        second_plan = analysis_plan("completed", "pending")
        planner = SequencePlanner(
            [
                act_decision(action_id="action-1", plan_value=first_plan),
                act_decision(
                    action_id="action-2",
                    plan_value=second_plan,
                    tool_name="catalog.inspect",
                ),
            ]
        )
        evaluator = SequenceEvaluator(
            [
                EvaluationDecision(
                    decision="continue",
                    evidence_sufficient=False,
                    completed_step_ids=("step-1",),
                    missing_evidence=("claim_2",),
                    contradictions=(),
                    rationale_summary="A second step remains.",
                ),
                EvaluationDecision(
                    decision="finish",
                    evidence_sufficient=True,
                    completed_step_ids=("step-1", "step-2"),
                    missing_evidence=(),
                    contradictions=(),
                    rationale_summary="Both steps are complete.",
                ),
            ]
        )
        tools = SequenceToolExecutor(["success", "success"])
        output = await build_analysis_agent_graph().ainvoke(
            graph_input(AgentMode.PREVIEW),
            context=graph_context(
                mode=AgentMode.PREVIEW,
                planner=planner,
                evaluator=evaluator,
                tools=tools,
            ),
        )
        self.assertTrue(output["final_response"].ok)
        self.assertEqual(output["budget"].tool_calls, 2)
        self.assertEqual(len(planner.calls), 2)
        self.assertEqual(
            [item.tool_name for item in output["observations"]],
            ["query.preview", "catalog.inspect"],
        )

    async def test_empty_preview_and_retryable_tool_failure_replan_within_bounds(self) -> None:
        for first_outcome in ("empty", "failed"):
            planner = SequencePlanner(
                [
                    act_decision(
                        action_id="first-action",
                        plan_value=analysis_plan("pending"),
                    ),
                    act_decision(
                        action_id="corrected-action",
                        plan_value=analysis_plan("pending", revision=2),
                    ),
                ]
            )
            tools = SequenceToolExecutor([first_outcome, "success"])
            output = await build_analysis_agent_graph().ainvoke(
                graph_input(AgentMode.PREVIEW, run_id=f"run-{first_outcome}"),
                context=graph_context(
                    mode=AgentMode.PREVIEW,
                    planner=planner,
                    tools=tools,
                ),
            )
            self.assertTrue(output["final_response"].ok)
            self.assertEqual(output["budget"].replans, 1)
            self.assertEqual(output["budget"].tool_calls, 2)

    async def test_model_failure_and_mode_bypass_end_safely(self) -> None:
        persisted: list[dict[str, object]] = []

        async def persist_turn(state) -> None:
            persisted.append(dict(state))

        failed = await build_analysis_agent_graph().ainvoke(
            graph_input(AgentMode.PREVIEW, run_id="run-invalid-model"),
            context=graph_context(
                mode=AgentMode.PREVIEW,
                planner=SequencePlanner([ValueError("invalid structured decision")]),
                persist_turn=persist_turn,
            ),
        )
        self.assertFalse(failed["final_response"].ok)
        self.assertEqual(failed["final_response"].error.code, ErrorCode.INTERNAL_ERROR)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["status"].value, "failed")
        self.assertEqual(persisted[0]["error"].code, ErrorCode.INTERNAL_ERROR)

        for mode, tool_name in (
            (AgentMode.PLAN, "query.preview"),
            (AgentMode.PREVIEW, "query.execute"),
        ):
            denied = await build_analysis_agent_graph().ainvoke(
                graph_input(mode, run_id=f"run-denied-{mode.value}"),
                context=graph_context(
                    mode=mode,
                    planner=SequencePlanner(
                        [
                            act_decision(
                                action_id="denied-action",
                                plan_value=analysis_plan("pending"),
                                tool_name=tool_name,
                            )
                        ]
                    ),
                ),
            )
            self.assertFalse(denied["final_response"].ok)
            self.assertEqual(
                denied["final_response"].error.code,
                ErrorCode.AGENT_ACTION_NOT_ALLOWED,
            )

    async def test_clarification_uses_native_interrupt_and_resume(self) -> None:
        plan_value = analysis_plan("pending")
        clarification = PlannerDecision(
            plan=plan_value,
            decision="clarify",
            clarification=AgentInputRequest(
                interrupt_id="interrupt-time-range",
                reason=AgentInputReason.CLARIFICATION,
                prompt="Which time range should be used?",
            ),
            rationale_summary="A time range is required.",
        )
        planner = SequencePlanner([clarification, finish_decision(plan_value)])
        saver = InMemorySaver()
        graph = build_analysis_agent_graph(checkpointer=saver)
        runtime_context = graph_context(mode=AgentMode.PLAN, planner=planner)
        config = {"configurable": {"thread_id": "run-interrupt"}}
        interrupted = await graph.ainvoke(
            graph_input(AgentMode.PLAN, run_id="run-interrupt"),
            context=runtime_context,
            config=config,
        )
        self.assertIn("__interrupt__", interrupted)
        self.assertEqual(
            interrupted["__interrupt__"][0].value["interrupt_id"],
            "interrupt-time-range",
        )

        resumed = await graph.ainvoke(
            Command(resume={"message": "Use the last 12 months"}),
            context=runtime_context,
            config=config,
        )
        self.assertTrue(resumed["final_response"].ok)
        self.assertIn("last 12 months", resumed["goal"].contextualized_question)

        waiting_events = [
            event
            async for event in build_analysis_agent_graph(
                checkpointer=InMemorySaver()
            ).astream_events(
                graph_input(AgentMode.PLAN, run_id="run-wait-events"),
                run_id="run-wait-events",
                context=graph_context(
                    mode=AgentMode.PLAN,
                    planner=SequencePlanner([clarification]),
                ),
                config={"configurable": {"thread_id": "run-wait-events"}},
            )
        ]
        self.assertEqual(waiting_events[-1].type, AgentEventType.RUN_WAITING)
        self.assertIsNone(waiting_events[-1].response)

    async def test_repeated_answered_clarification_fails_with_typed_loop_error(
        self,
    ) -> None:
        plan_value = analysis_plan("pending")
        clarification = PlannerDecision(
            plan=plan_value,
            decision="clarify",
            clarification=AgentInputRequest(
                interrupt_id="interrupt-definition",
                reason=AgentInputReason.CLARIFICATION,
                prompt="Which definition should be used?",
            ),
            rationale_summary="A definition is required.",
        )
        saver = InMemorySaver()
        graph = build_analysis_agent_graph(checkpointer=saver)
        runtime_context = graph_context(
            mode=AgentMode.PLAN,
            planner=SequencePlanner([clarification, clarification]),
        )
        config = {"configurable": {"thread_id": "run-clarification-loop"}}
        await graph.ainvoke(
            graph_input(AgentMode.PLAN, run_id="run-clarification-loop"),
            context=runtime_context,
            config=config,
        )

        resumed = await graph.ainvoke(
            Command(resume={"message": "Use the documented definition"}),
            context=runtime_context,
            config=config,
        )

        self.assertFalse(resumed["final_response"].ok)
        self.assertEqual(
            resumed["final_response"].error.code,
            ErrorCode.AGENT_CLARIFICATION_LOOP,
        )
        self.assertEqual(len(resumed["clarification_turns"]), 1)

    async def test_model_call_is_cancelled_at_the_run_deadline(self) -> None:
        class SlowPlanner:
            async def decide(self, **kwargs):
                del kwargs
                await asyncio.sleep(5)
                raise AssertionError("deadline did not cancel the model call")

        limits = AgentRunBudget(max_duration_seconds=1)
        started = time.monotonic()
        output = await build_analysis_agent_graph(budget_limits=limits).ainvoke(
            graph_input(AgentMode.PLAN, run_id="run-model-deadline"),
            context=graph_context(
                mode=AgentMode.PLAN,
                planner=SlowPlanner(),
                budget_limits=limits,
            ),
        )

        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(
            output["final_response"].error.code,
            ErrorCode.DEADLINE_EXCEEDED,
        )

    async def test_execute_mode_can_enter_full_query_tool_through_guard(self) -> None:
        output = await build_analysis_agent_graph().ainvoke(
            graph_input(AgentMode.EXECUTE, run_id="run-execute-mode"),
            context=graph_context(
                mode=AgentMode.EXECUTE,
                planner=SequencePlanner(
                    [
                        act_decision(
                            action_id="execute-action",
                            plan_value=analysis_plan("pending"),
                            tool_name="query.execute",
                        )
                    ]
                ),
                tools=SequenceToolExecutor(["success"]),
            ),
        )
        self.assertTrue(output["final_response"].ok)
        self.assertEqual(output["budget"].query_executes, 1)

    async def test_budget_deadline_and_cancel_have_finite_terminal_routes(self) -> None:
        limits = AgentRunBudget(max_agent_steps=1, max_model_calls=5)
        planner = SequencePlanner(
            [
                act_decision(action_id="action-1", plan_value=analysis_plan("pending")),
                act_decision(action_id="action-2", plan_value=analysis_plan("pending")),
            ]
        )
        evaluator = SequenceEvaluator(
            [
                EvaluationDecision(
                    decision="continue",
                    evidence_sufficient=False,
                    completed_step_ids=("step-1",),
                    missing_evidence=("more",),
                    contradictions=(),
                    rationale_summary="Continue.",
                )
            ]
        )
        budgeted = await build_analysis_agent_graph(budget_limits=limits).ainvoke(
            graph_input(AgentMode.PREVIEW, run_id="run-budget"),
            context=graph_context(
                mode=AgentMode.PREVIEW,
                planner=planner,
                evaluator=evaluator,
                budget_limits=limits,
            ),
        )
        self.assertEqual(
            budgeted["final_response"].error.code,
            ErrorCode.AGENT_MAX_STEPS_EXCEEDED,
        )

        cancelled = await build_analysis_agent_graph().ainvoke(
            graph_input(AgentMode.PREVIEW, run_id="run-cancelled"),
            context=graph_context(
                mode=AgentMode.PREVIEW,
                planner=SequencePlanner([]),
                cancelled=lambda: True,
            ),
        )
        self.assertEqual(cancelled["status"].value, "cancelled")

        start = datetime.now(UTC)
        times = iter((start, start + timedelta(seconds=1000)))
        deadline_limits = AgentRunBudget(max_duration_seconds=1)
        expired = await build_analysis_agent_graph(
            budget_limits=deadline_limits
        ).ainvoke(
            graph_input(AgentMode.PREVIEW, run_id="run-deadline"),
            context=graph_context(
                mode=AgentMode.PREVIEW,
                planner=SequencePlanner([]),
                budget_limits=deadline_limits,
                clock=lambda: next(times),
            ),
        )
        self.assertEqual(
            expired["final_response"].error.code,
            ErrorCode.DEADLINE_EXCEEDED,
        )

    def test_graph_topology_has_guarded_dynamic_cycles_and_finite_recursion(self) -> None:
        limits = AgentRunBudget(max_agent_steps=3, max_replans=2)
        graph = build_analysis_agent_graph(budget_limits=limits)
        document = graph.compiled_graph.get_graph().to_json()
        inbound_execute = [
            edge["source"]
            for edge in document["edges"]
            if edge["target"] == "execute_tool"
        ]
        self.assertEqual(inbound_execute, ["guard_decision"])
        self.assertIn(
            {"source": "evaluate_progress", "target": "plan_or_replan", "conditional": True},
            document["edges"],
        )
        self.assertEqual(graph.recursion_limit, analysis_graph_recursion_limit(limits))
        self.assertGreater(graph.recursion_limit, limits.max_agent_steps)
        self.assertLess(graph.recursion_limit, 1000)


if __name__ == "__main__":
    unittest.main()
