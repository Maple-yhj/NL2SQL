from __future__ import annotations

from functools import partial

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from core.environment import load_project_environment

load_project_environment()

from core.embeddings import EmbeddingClientProtocol, create_embedding_client
from core.llm import LLMProtocol, create_llm
from graph.context import GraphContext
from graph.memory_store import ConversationStoreProtocol, create_conversation_store
from graph.node import (
    contextualize_question_node,
    execute_sql_node,
    explain_node,
    finalize_node,
    generate_sql_node,
    initialize_node,
    load_memory_node,
    parse_intent_node,
    persist_memory_node,
    search_metrics_node,
    search_schema_node,
    validate_sql_node,
)
from graph.router import route_after_execute, route_after_schema, route_after_validate
from graph.state import GraphState, InputState, OutputState


def _route_next(state: GraphState, next_node: str) -> str:
    return "persist_memory" if state.get("error") else next_node


def _route_schema(state: GraphState) -> str:
    destination = route_after_schema(state)
    return "persist_memory" if destination == "finalize" else destination


def _route_validation(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> str:
    destination = route_after_validate(
        state,
        max_attempts=runtime.context.max_validation_attempts,
    )
    return "persist_memory" if destination == "finalize" else destination


def _route_execute(state: GraphState) -> str:
    destination = route_after_execute(state)
    return "persist_memory" if destination == "finalize" else destination


def build_graph():
    builder = StateGraph(
        GraphState,
        input_schema=InputState,
        output_schema=OutputState,
        context_schema=GraphContext,
    )
    builder.add_node("initialize", initialize_node)
    builder.add_node("load_memory", load_memory_node)
    builder.add_node("contextualize_question", contextualize_question_node)
    builder.add_node("parse_intent", parse_intent_node)
    builder.add_node("search_metrics", search_metrics_node)
    builder.add_node("search_schema", search_schema_node)
    builder.add_node("generate_sql", generate_sql_node)
    builder.add_node("validate_sql", validate_sql_node)
    builder.add_node("execute_sql", execute_sql_node)
    builder.add_node("explain", explain_node)
    builder.add_node("persist_memory", persist_memory_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "initialize")
    builder.add_conditional_edges(
        "initialize",
        partial(_route_next, next_node="load_memory"),
        ["load_memory", "persist_memory"],
    )
    builder.add_edge("load_memory", "contextualize_question")
    builder.add_conditional_edges(
        "contextualize_question",
        partial(_route_next, next_node="parse_intent"),
        ["parse_intent", "persist_memory"],
    )
    builder.add_conditional_edges(
        "parse_intent",
        partial(_route_next, next_node="search_metrics"),
        ["search_metrics", "persist_memory"],
    )
    builder.add_conditional_edges(
        "search_metrics",
        partial(_route_next, next_node="search_schema"),
        ["search_schema", "persist_memory"],
    )
    builder.add_conditional_edges(
        "search_schema",
        _route_schema,
        ["generate_sql", "persist_memory"],
    )
    builder.add_conditional_edges(
        "generate_sql",
        partial(_route_next, next_node="validate_sql"),
        ["validate_sql", "persist_memory"],
    )
    builder.add_conditional_edges(
        "validate_sql",
        _route_validation,
        ["generate_sql", "execute_sql", "persist_memory"],
    )
    builder.add_conditional_edges(
        "execute_sql",
        _route_execute,
        ["explain", "persist_memory"],
    )
    builder.add_edge("explain", "persist_memory")
    builder.add_edge("persist_memory", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


graph = build_graph()


async def run_nl2sql(
    question: str,
    tenant_id: str = "demo",
    *,
    execute: bool = False,
    conversation_id: str = "",
    user_id: str = "",
    llm: LLMProtocol | None = None,
    embeddings: EmbeddingClientProtocol | None = None,
    memory_store: ConversationStoreProtocol | None = None,
    dsn: str | None = None,
    memory_dsn: str | None = None,
    timeout_ms: int = 10_000,
    max_limit: int = 1000,
    max_validation_attempts: int = 2,
    memory_history_limit: int = 8,
) -> OutputState:
    if max_validation_attempts <= 0:
        raise ValueError("max_validation_attempts must be positive")
    if memory_history_limit < 0:
        raise ValueError("memory_history_limit must be non-negative")
    context = GraphContext(
        llm=llm or create_llm(),
        embeddings=embeddings or create_embedding_client(),
        memory_store=memory_store or create_conversation_store(memory_dsn),
        dsn=dsn,
        memory_dsn=memory_dsn,
        timeout_ms=timeout_ms,
        max_limit=max_limit,
        max_validation_attempts=max_validation_attempts,
        memory_history_limit=memory_history_limit,
    )
    input_state: InputState = {
        "question": question,
        "tenant_id": tenant_id,
        "execute": execute,
    }
    if conversation_id:
        input_state["conversation_id"] = conversation_id
    if user_id:
        input_state["user_id"] = user_id
    config = {"recursion_limit": 16 + max_validation_attempts * 2}
    if conversation_id:
        config["configurable"] = {"thread_id": conversation_id}
    return await graph.ainvoke(
        input_state,
        context=context,
        config=config,
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
        conversation_id=input_state.get("conversation_id", ""),
        user_id=input_state.get("user_id", ""),
        llm=llm,
        embeddings=embeddings,
    )
