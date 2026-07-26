"""LangGraph Studio shell around the public Data Agent Runtime stream."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from data_agent.runtime import AgentRequest, AgentResponse, PrincipalContext


RuntimeFactory = Callable[[], Awaitable[Any]]


class StudioInput(TypedDict):
    request: AgentRequest | Mapping[str, Any]
    principal: PrincipalContext | Mapping[str, Any]


class StudioOutput(TypedDict):
    ok: bool
    question: str
    contextualized_question: str | None
    conversation_id: str | None
    tenant_id: str | None
    logical_plan: dict[str, Any] | None
    sql: str | None
    message_type: str
    rows: list[dict[str, Any]]
    chart: dict[str, Any] | None
    answer: str | None
    error: dict[str, Any] | None
    trace: list[dict[str, Any]]
    pending_memory_updates: list[dict[str, Any]]
    version_pins: dict[str, Any] | None


class StudioState(TypedDict, total=False):
    request: AgentRequest | Mapping[str, Any]
    principal: PrincipalContext | Mapping[str, Any]
    ok: bool
    question: str
    contextualized_question: str | None
    conversation_id: str | None
    tenant_id: str | None
    logical_plan: dict[str, Any] | None
    sql: str | None
    message_type: str
    rows: list[dict[str, Any]]
    chart: dict[str, Any] | None
    answer: str | None
    error: dict[str, Any] | None
    trace: list[dict[str, Any]]
    pending_memory_updates: list[dict[str, Any]]
    version_pins: dict[str, Any] | None


async def _default_runtime_factory() -> Any:
    from data_agent.runtime.composition_root import build_olist_runtime

    return await build_olist_runtime()


def build_studio_graph(runtime_factory: RuntimeFactory | None = None):
    factory = runtime_factory or _default_runtime_factory

    async def invoke_runtime(state: StudioInput) -> StudioOutput:
        request = AgentRequest.model_validate(state["request"])
        principal = PrincipalContext.model_validate(state["principal"])
        composition = await factory()
        try:
            terminal: AgentResponse | None = None
            async for event in composition.runtime.run(request, principal):
                if event.response is None:
                    continue
                if terminal is not None:
                    raise RuntimeError("runtime emitted more than one terminal response")
                terminal = event.response
            if terminal is None:
                raise RuntimeError("runtime stream ended without a terminal response")
            return cast(StudioOutput, terminal.model_dump(mode="json"))
        finally:
            await composition.close()

    builder = StateGraph(
        StudioState,
        input_schema=StudioInput,
        output_schema=StudioOutput,
    )
    builder.add_node("runtime", invoke_runtime)
    builder.add_edge(START, "runtime")
    builder.add_edge("runtime", END)
    return builder.compile(name="data-agent-runtime")


graph = build_studio_graph()


__all__ = ["build_studio_graph", "graph"]
