"""Native LangGraph topology for the iterative data-analysis Agent."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import TypeAdapter, ValidationError

from data_agent.runtime.events import (
    AgentEvent,
    AgentEventPayload,
    AgentEventType,
    RunCompletedPayload,
    RunFailedPayload,
    RunWaitingPayload,
)
from data_agent.runtime.models import ComponentVersionPin, DatasetRuntimeVersionPins

from .models import AgentRunBudget, DatasetAuthority, stable_digest
from .nodes import (
    AnalysisGraphContext,
    evaluate_progress,
    execute_tool,
    fail,
    finalize_run,
    guard_decision,
    initialize_run,
    load_context,
    observe_result,
    persist_turn,
    plan_or_replan,
    request_input,
    synthesize_answer,
    validate_answer,
)
from .routing import route_after_evaluation, route_after_guard
from .state import AnalysisAgentState


ANALYSIS_GRAPH_ID = "dataset-analysis-agent"
ANALYSIS_GRAPH_VERSION = "1.0.0"
_TOPOLOGY = {
    "entry": "initialize_run",
    "nodes": (
        "initialize_run",
        "load_context",
        "plan_or_replan",
        "guard_decision",
        "execute_tool",
        "observe_result",
        "evaluate_progress",
        "request_input",
        "synthesize_answer",
        "validate_answer",
        "persist_turn",
        "finalize_run",
        "fail",
    ),
    "fixed_edges": (
        ("START", "initialize_run"),
        ("initialize_run", "load_context"),
        ("load_context", "plan_or_replan"),
        ("plan_or_replan", "guard_decision"),
        ("execute_tool", "observe_result"),
        ("observe_result", "evaluate_progress"),
        ("synthesize_answer", "validate_answer"),
        ("validate_answer", "persist_turn"),
        ("persist_turn", "finalize_run"),
        ("finalize_run", "END"),
        ("fail", "END"),
    ),
    "dynamic_edges": {
        "guard_decision": (
            "execute_tool",
            "request_input",
            "synthesize_answer",
            "fail",
        ),
        "evaluate_progress": (
            "plan_or_replan",
            "request_input",
            "synthesize_answer",
            "fail",
        ),
        "request_input": ("plan_or_replan",),
    },
}
ANALYSIS_GRAPH_DIGEST = stable_digest(_TOPOLOGY)
LANGGRAPH_RECURSION_MARGIN = 12
_EVENT_PAYLOAD_ADAPTER = TypeAdapter(AgentEventPayload)


def analysis_graph_recursion_limit(limits: AgentRunBudget) -> int:
    return (
        LANGGRAPH_RECURSION_MARGIN
        + limits.max_agent_steps * 6
        + limits.max_replans * 2
    )


def build_dataset_version_pins(
    *,
    authority: DatasetAuthority,
    tool_registry_version: str,
    model_versions: tuple[ComponentVersionPin, ...],
    relationship_graph_digest: str | None = None,
    analysis_skill_id: str = "dataset.analytics",
    analysis_skill_version: str = "1.0.0",
    domain_pack_id: str | None = None,
    domain_pack_version: str | None = None,
    domain_pack_digest: str | None = None,
    runtime_version: str = "0.1.0",
) -> DatasetRuntimeVersionPins:
    return DatasetRuntimeVersionPins(
        runtime_version=runtime_version,
        graph_id=ANALYSIS_GRAPH_ID,
        graph_version=ANALYSIS_GRAPH_VERSION,
        graph_digest=ANALYSIS_GRAPH_DIGEST,
        tool_registry_version=tool_registry_version,
        analysis_skill_id=analysis_skill_id,
        analysis_skill_version=analysis_skill_version,
        domain_pack_id=domain_pack_id,
        domain_pack_version=domain_pack_version,
        domain_pack_digest=domain_pack_digest,
        model_versions=model_versions,
        source_id=authority.source_id,
        source_version=authority.source_version,
        binding_id=authority.binding_id,
        binding_version=authority.binding_version,
        metric_set_id=authority.metric_set_id,
        metric_set_version=authority.metric_set_version,
        metric_set_digest=authority.metric_set_digest,
        metric_overlay_id=authority.metric_overlay_id,
        metric_overlay_digest=authority.metric_overlay_digest,
        schema_fingerprint=authority.schema_fingerprint,
        relationship_graph_digest=relationship_graph_digest,
    )


@dataclass(frozen=True, slots=True)
class CompiledAnalysisGraph:
    compiled_graph: Any
    recursion_limit: int
    graph_id: str = ANALYSIS_GRAPH_ID
    graph_version: str = ANALYSIS_GRAPH_VERSION
    graph_digest: str = ANALYSIS_GRAPH_DIGEST

    async def ainvoke(
        self,
        value: AnalysisAgentState | dict[str, object] | Any,
        *,
        context: AnalysisGraphContext,
        config: dict[str, Any] | None = None,
    ) -> AnalysisAgentState:
        effective = dict(config or {})
        effective.setdefault("recursion_limit", self.recursion_limit)
        return await self.compiled_graph.ainvoke(
            value,
            config=effective,
            context=context,
        )

    async def astream(
        self,
        value: AnalysisAgentState | dict[str, object] | Any,
        *,
        context: AnalysisGraphContext,
        config: dict[str, Any] | None = None,
        stream_mode: str | tuple[str, ...] = "updates",
    ):
        effective = dict(config or {})
        effective.setdefault("recursion_limit", self.recursion_limit)
        async for item in self.compiled_graph.astream(
            value,
            config=effective,
            context=context,
            stream_mode=stream_mode,
        ):
            yield item

    async def astream_events(
        self,
        value: AnalysisAgentState | dict[str, object] | Any,
        *,
        run_id: str,
        context: AnalysisGraphContext,
        config: dict[str, Any] | None = None,
        start_sequence: int = 0,
    ):
        """Translate LangGraph custom/update chunks into only public AgentEvents."""

        sequence = start_sequence
        pending_terminal: RunCompletedPayload | RunFailedPayload | None = None
        async for item in self.astream(
            value,
            context=context,
            config=config,
            stream_mode=("custom", "updates"),
        ):
            payload = _event_payload(item)
            if payload is not None:
                if isinstance(payload, (RunCompletedPayload, RunFailedPayload)):
                    if pending_terminal is not None:
                        raise RuntimeError("analysis graph emitted multiple terminal payloads")
                    pending_terminal = payload
                    continue
                yield AgentEvent(
                    type=AgentEventType(payload.kind),
                    run_id=run_id,
                    sequence=sequence,
                    data=payload,
                )
                sequence += 1
                continue
            waiting = _waiting_payload(item)
            if waiting is not None:
                yield AgentEvent(
                    type=AgentEventType.RUN_WAITING,
                    run_id=run_id,
                    sequence=sequence,
                    data=waiting,
                )
                sequence += 1
                continue
            if pending_terminal is not None:
                response = _terminal_response(item)
                if response is not None:
                    yield AgentEvent(
                        type=AgentEventType(pending_terminal.kind),
                        run_id=run_id,
                        sequence=sequence,
                        data=pending_terminal,
                        response=response,
                    )
                    sequence += 1
                    pending_terminal = None
        if pending_terminal is not None:
            raise RuntimeError("analysis graph terminal payload had no terminal response")


def build_analysis_agent_graph(
    *,
    checkpointer: object | None = None,
    budget_limits: AgentRunBudget | None = None,
    default_context: AnalysisGraphContext | None = None,
) -> CompiledAnalysisGraph:
    limits = budget_limits or AgentRunBudget()
    builder = StateGraph(
        AnalysisAgentState,
        context_schema=(AnalysisGraphContext if default_context is None else None),
    )
    bind = _bind_default_context(default_context)
    builder.add_node("initialize_run", bind(initialize_run))
    builder.add_node("load_context", bind(load_context))
    builder.add_node("plan_or_replan", bind(plan_or_replan))
    builder.add_node("guard_decision", bind(guard_decision))
    builder.add_node("execute_tool", bind(execute_tool))
    builder.add_node("observe_result", bind(observe_result))
    builder.add_node("evaluate_progress", bind(evaluate_progress))
    builder.add_node("request_input", bind(request_input))
    builder.add_node("synthesize_answer", bind(synthesize_answer))
    builder.add_node("validate_answer", bind(validate_answer))
    builder.add_node("persist_turn", bind(persist_turn))
    builder.add_node("finalize_run", bind(finalize_run))
    builder.add_node("fail", bind(fail))

    builder.add_edge(START, "initialize_run")
    builder.add_conditional_edges(
        "initialize_run",
        lambda state: _next_or_fail(state, "load_context"),
        {"load_context": "load_context", "fail": "fail"},
    )
    builder.add_conditional_edges(
        "load_context",
        lambda state: _next_or_fail(state, "plan_or_replan"),
        {"plan_or_replan": "plan_or_replan", "fail": "fail"},
    )
    builder.add_edge("plan_or_replan", "guard_decision")
    builder.add_conditional_edges(
        "guard_decision",
        route_after_guard,
        {
            "execute_tool": "execute_tool",
            "request_input": "request_input",
            "synthesize_answer": "synthesize_answer",
            "fail": "fail",
        },
    )
    builder.add_conditional_edges(
        "execute_tool",
        lambda state: _next_or_fail(state, "observe_result"),
        {"observe_result": "observe_result", "fail": "fail"},
    )
    builder.add_conditional_edges(
        "observe_result",
        lambda state: _next_or_fail(state, "evaluate_progress"),
        {"evaluate_progress": "evaluate_progress", "fail": "fail"},
    )
    builder.add_conditional_edges(
        "evaluate_progress",
        route_after_evaluation,
        {
            "plan_or_replan": "plan_or_replan",
            "request_input": "request_input",
            "synthesize_answer": "synthesize_answer",
            "fail": "fail",
        },
    )
    builder.add_edge("request_input", "plan_or_replan")
    builder.add_conditional_edges(
        "synthesize_answer",
        lambda state: _next_or_fail(state, "validate_answer"),
        {"validate_answer": "validate_answer", "fail": "fail"},
    )
    builder.add_conditional_edges(
        "validate_answer",
        lambda state: _next_or_fail(state, "persist_turn"),
        {"persist_turn": "persist_turn", "fail": "fail"},
    )
    builder.add_conditional_edges(
        "persist_turn",
        lambda state: _next_or_fail(state, "finalize_run"),
        {"finalize_run": "finalize_run", "fail": "fail"},
    )
    builder.add_conditional_edges(
        "finalize_run",
        lambda state: "fail" if state.get("error") is not None else END,
        {"fail": "fail", END: END},
    )
    builder.add_edge("fail", END)
    compiled = builder.compile(
        checkpointer=checkpointer,
        name=f"{ANALYSIS_GRAPH_ID}@{ANALYSIS_GRAPH_VERSION}",
    )
    return CompiledAnalysisGraph(
        compiled_graph=compiled,
        recursion_limit=analysis_graph_recursion_limit(limits),
    )


def _bind_default_context(
    default_context: AnalysisGraphContext | None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Bind an inert composition without exposing it as Studio input schema."""

    def bind(node: Callable[..., Any]) -> Callable[..., Any]:
        if default_context is None:
            return node

        async def bound(
            state: AnalysisAgentState,
            runtime: Runtime[None],
        ) -> dict[str, object]:
            return await node(state, replace(runtime, context=default_context))

        return bound

    return bind


def _next_or_fail(state: AnalysisAgentState, expected: str) -> str:
    if state.get("error") is not None:
        return "fail"
    if state.get("next_route") != expected:
        raise ValueError("analysis node returned an invalid server route")
    return expected


def _event_payload(value: object):
    try:
        return _EVENT_PAYLOAD_ADAPTER.validate_python(value)
    except (TypeError, ValueError, ValidationError):
        return None


def _terminal_response(value: object):
    if not isinstance(value, dict):
        return None
    for nested in value.values():
        if not isinstance(nested, dict):
            continue
        response = nested.get("final_response")
        if response is not None:
            from data_agent.runtime.models import AgentResponse

            return AgentResponse.model_validate(response)
    return None


def _waiting_payload(value: object) -> RunWaitingPayload | None:
    if not isinstance(value, dict) or "__interrupt__" not in value:
        return None
    interrupts = value["__interrupt__"]
    if not isinstance(interrupts, (list, tuple)) or len(interrupts) != 1:
        raise RuntimeError("analysis graph emitted an invalid interrupt set")
    interrupt_value = getattr(interrupts[0], "value", None)
    if interrupt_value is None:
        raise RuntimeError("analysis graph interrupt omitted its typed payload")
    from .models import AgentInputRequest

    return RunWaitingPayload(
        input_request=AgentInputRequest.model_validate(interrupt_value)
    )


__all__ = [
    "ANALYSIS_GRAPH_DIGEST",
    "ANALYSIS_GRAPH_ID",
    "ANALYSIS_GRAPH_VERSION",
    "CompiledAnalysisGraph",
    "analysis_graph_recursion_limit",
    "build_analysis_agent_graph",
    "build_dataset_version_pins",
]
