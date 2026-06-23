from __future__ import annotations

from functools import partial

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from core.environment import load_project_environment

load_project_environment()

from core.embeddings import EmbeddingClientProtocol, create_embedding_client
from core.llm import LLMProtocol, create_llm
from graph.context import GraphContext
from graph.node import (
    execute_sql_node,
    explain_node,
    finalize_node,
    generate_sql_node,
    initialize_node,
    parse_intent_node,
    search_metrics_node,
    search_schema_node,
    validate_sql_node,
)
from graph.router import route_after_execute, route_after_schema, route_after_validate
from graph.state import GraphState, InputState, OutputState


def _route_next(state: GraphState, next_node: str) -> str:
    return "finalize" if state.get("error") else next_node


def _route_validation(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> str:
    return route_after_validate(
        state,
        max_attempts=runtime.context.max_validation_attempts,
    )


def build_graph():
    builder = StateGraph(
        GraphState,
        input_schema=InputState,
        output_schema=OutputState,
        context_schema=GraphContext,
    )
    builder.add_node("initialize", initialize_node)
    builder.add_node("parse_intent", parse_intent_node)
    builder.add_node("search_metrics", search_metrics_node)
    builder.add_node("search_schema", search_schema_node)
    builder.add_node("generate_sql", generate_sql_node)
    builder.add_node("validate_sql", validate_sql_node)
    builder.add_node("execute_sql", execute_sql_node)
    builder.add_node("explain", explain_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "initialize")
    builder.add_conditional_edges(
        "initialize",
        partial(_route_next, next_node="parse_intent"),
        ["parse_intent", "finalize"],
    )
    builder.add_conditional_edges(
        "parse_intent",
        partial(_route_next, next_node="search_metrics"),
        ["search_metrics", "finalize"],
    )
    builder.add_conditional_edges(
        "search_metrics",
        partial(_route_next, next_node="search_schema"),
        ["search_schema", "finalize"],
    )
    builder.add_conditional_edges(
        "search_schema",
        route_after_schema,
        ["generate_sql", "finalize"],
    )
    builder.add_conditional_edges(
        "generate_sql",
        partial(_route_next, next_node="validate_sql"),
        ["validate_sql", "finalize"],
    )
    builder.add_conditional_edges(
        "validate_sql",
        _route_validation,
        ["generate_sql", "execute_sql", "finalize"],
    )
    builder.add_conditional_edges(
        "execute_sql",
        route_after_execute,
        ["explain", "finalize"],
    )
    builder.add_edge("explain", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


graph = build_graph()


async def run_nl2sql(
    question: str,
    tenant_id: str = "demo",
    *,
    execute: bool = False,
    llm: LLMProtocol | None = None,
    embeddings: EmbeddingClientProtocol | None = None,
    dsn: str | None = None,
    timeout_ms: int = 10_000,
    max_limit: int = 1000,
    max_validation_attempts: int = 2,
) -> OutputState:
    if max_validation_attempts <= 0:
        raise ValueError("max_validation_attempts must be positive")
    context = GraphContext(
        llm=llm or create_llm(),
        embeddings=embeddings or create_embedding_client(),
        dsn=dsn,
        timeout_ms=timeout_ms,
        max_limit=max_limit,
        max_validation_attempts=max_validation_attempts,
    )
    return await graph.ainvoke(
        {"question": question, "tenant_id": tenant_id, "execute": execute},
        context=context,
        config={"recursion_limit": 12 + max_validation_attempts * 2},
    )


async def run_graph(
    input_state: InputState,
    *,
    llm: LLMProtocol,
    embeddings: EmbeddingClientProtocol,
) -> OutputState:
    return await run_nl2sql(
        input_state["question"],
        input_state["tenant_id"],
        execute=input_state["execute"],
        llm=llm,
        embeddings=embeddings,
    )
