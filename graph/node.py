from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from langgraph.runtime import Runtime

from engine.intent_parser import parse_intent
from graph.context import GraphContext
from graph.state import GraphState, OutputState
from graph.tools.contextualize_question import contextualize_question
from graph.tools.execute_sql import execute_sql
from graph.tools.explain_result import explain_result
from graph.tools.sql_generator import generate_sql
from graph.tools.sql_store import search_metrics, search_schema
from graph.tools.validate_sql import validate_sql


_JOIN_TABLE_RE = re.compile(r"\bJOIN\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.IGNORECASE)


def _trace(state: GraphState, node_name: str, *, ok: bool, message: str = "") -> list[dict[str, Any]]:
    return [
        *state.get("trace", []),
        {"node": node_name, "ok": ok, "message": message},
    ]


def _error(state: GraphState, node_name: str, exc: Exception | str) -> GraphState:
    message = str(exc)
    return {
        "error": message,
        "trace": _trace(state, node_name, ok=False, message=message),
    } # type: ignore


def _question_for_model(state: GraphState) -> str:
    return state.get("contextualized_question") or state["question"]


def _state_ok(state: GraphState) -> bool:
    sql = state.get("validated_sql", "")
    error = state.get("error", "")
    ok = bool(sql) and not error
    if state.get("execute", False):
        ok = ok and (state.get("execution_result") or {}).get("ok") is True
    return ok


def _metric_table_names(metrics_result: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for metric in metrics_result.get("metrics", []) or []:
        base_table = str(metric.get("base_table") or "").strip()
        if base_table:
            name = base_table.split()[0].split(".")[-1]
            if name not in names:
                names.append(name)
        for join_clause in metric.get("join_tables", []) or []:
            for match in _JOIN_TABLE_RE.finditer(str(join_clause)):
                name = match.group(1).split(".")[-1]
                if name not in names:
                    names.append(name)
    return names


def _retry_feedback(sql: str, validation: dict[str, Any]) -> str:
    violations = "\n".join(
        f"- {item.get('code')}: {item.get('message')}"
        for item in validation.get("violations", []) or []
    ) or "- none"
    warnings = "\n".join(
        f"- {item}" for item in validation.get("warnings", []) or []
    ) or "- none"
    return (
        f"Previous SQL:\n{sql}\n\n"
        f"Validation message:\n{validation.get('message', '')}\n\n"
        f"Violations:\n{violations}\n\nWarnings:\n{warnings}"
    )


async def initialize_node(state: GraphState) -> GraphState:
    question = state.get("question", "").strip()
    tenant_id = state.get("tenant_id", "").strip()
    conversation_id = state.get("conversation_id", "").strip()
    user_id = state.get("user_id", "").strip()
    if not question:
        return _error(state, "initialize", "question is empty")
    if not tenant_id:
        return _error(state, "initialize", "tenant_id is empty")
    return {
        "question": question,
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "contextualized_question": question,
        "conversation_history": [],
        "user_memories": [],
        "validation_attempts": 0,
        "rows": [],
        "answer": "",
        "error": "",
        "trace": [{"node": "initialize", "ok": True, "message": "success"}],
    } # type: ignore


async def load_memory_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    conversation_id = state.get("conversation_id", "")
    user_id = state.get("user_id", "")
    if not conversation_id and not user_id:
        return {
            "conversation_history": [],
            "user_memories": [],
            "trace": _trace(state, "load_memory", ok=True, message="memory disabled"),
        } # type: ignore
    try:
        context = await runtime.context.memory_store.load_context(
            tenant_id=state["tenant_id"],
            conversation_id=conversation_id,
            user_id=user_id,
            limit=runtime.context.memory_history_limit,
        )
        history = context.get("history", []) or []
        memories = context.get("user_memories", []) or []
        return {
            "conversation_history": history,
            "user_memories": memories,
            "trace": _trace(
                state,
                "load_memory",
                ok=True,
                message=f"loaded {len(history)} history item(s), {len(memories)} user memory item(s)",
            ),
        } # type: ignore
    except Exception as exc:
        return {
            "conversation_history": [],
            "user_memories": [],
            "trace": _trace(state, "load_memory", ok=False, message=str(exc)),
        } # type: ignore


async def contextualize_question_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        rewritten = await contextualize_question(
            question=state["question"],
            conversation_history=state.get("conversation_history", []),
            user_memories=state.get("user_memories", []),
            llm=runtime.context.llm,
        )
        return {
            "contextualized_question": rewritten,
            "trace": _trace(
                state,
                "contextualize_question",
                ok=True,
                message="unchanged" if rewritten == state["question"] else "rewritten",
            ),
        } # type: ignore
    except Exception as exc:
        return {
            "contextualized_question": state["question"],
            "trace": _trace(state, "contextualize_question", ok=False, message=str(exc)),
        } # type: ignore


async def parse_intent_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        intent = await parse_intent(_question_for_model(state), llm=runtime.context.llm)
        return {
            "intent": intent,
            "trace": _trace(state, "parse_intent", ok=True, message="success"),
        } # pyright: ignore[reportReturnType]
    except Exception as exc:
        return _error(state, "parse_intent", exc)


async def search_metrics_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        result = await search_metrics(
            query=_question_for_model(state),
            tenant_id=state["tenant_id"],
            embedding_client=runtime.context.embeddings,
        )
        return {
            "metrics_result": result,
            "table_names": _metric_table_names(result),
            "trace": _trace(
                state,
                "search_metrics",
                ok=bool(result.get("ok")),
                message=str(result.get("message", "")),
            ),
        } # type: ignore
    except Exception as exc:
        return _error(state, "search_metrics", exc)


async def search_schema_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        result = await search_schema(
            query=_question_for_model(state),
            tenant_id=state["tenant_id"],
            embedding_client=runtime.context.embeddings,
            table_names=state.get("table_names") or None,
        )
        allowed_tables = [
            str(item["table_name"])
            for item in result.get("schema", []) or []
            if item.get("table_name")
        ]
        update: GraphState = {
            "schema_result": result,
            "allowed_tables": allowed_tables,
            "trace": _trace(
                state,
                "search_schema",
                ok=bool(result.get("ok") and allowed_tables),
                message=str(result.get("message", "")),
            ),
        } # type: ignore
        if not allowed_tables:
            update["error"] = "No authorized schema tables were retrieved."
        return update
    except Exception as exc:
        return _error(state, "search_schema", exc)


async def generate_sql_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        sql = await generate_sql(
            question=_question_for_model(state),
            intent=state.get("intent"),
            metrics_result=state.get("metrics_result", {}),
            schema_result=state.get("schema_result", {}),
            retry_feedback=state.get("retry_feedback"),
            conversation_history=state.get("conversation_history", []),
            user_memories=state.get("user_memories", []),
            llm=runtime.context.llm,
        )
        return {
            "candidate_sql": sql,
            "validated_sql": "",
            "validation_result": {},
            "execution_result": {},
            "rows": [],
            "answer": "",
            "error": "",
            "trace": _trace(state, "generate_sql", ok=True, message="success"),
        } # type: ignore
    except Exception as exc:
        return _error(state, "generate_sql", exc)


async def validate_sql_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    allowed_tables = state.get("allowed_tables") or []
    if not allowed_tables:
        return _error(state, "validate_sql", "allowed_tables is empty")
    try:
        result = await validate_sql(
            sql=state.get("candidate_sql", ""),
            tenant_id=state["tenant_id"],
            allowed_tables=allowed_tables,
            max_limit=runtime.context.max_limit,
        )
        attempts = state.get("validation_attempts", 0) + 1
        ok = result.get("ok") is True
        return {
            "validation_result": result,
            "validation_attempts": attempts,
            "validated_sql": str(result.get("normalized_sql", "")) if ok else "",
            "retry_feedback": "" if ok else _retry_feedback(state.get("candidate_sql", ""), result),
            "execution_result": {},
            "rows": [],
            "answer": "",
            "error": "" if ok else str(result.get("message", "SQL validation failed.")),
            "trace": _trace(
                state,
                "validate_sql",
                ok=ok,
                message=str(result.get("message", "")),
            ),
        } # type: ignore
    except Exception as exc:
        return _error(state, "validate_sql", exc)


async def execute_sql_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        result = await execute_sql(
            sql=state.get("validated_sql", ""),
            tenant_id=state["tenant_id"],
            dsn=runtime.context.dsn,
            timeout_ms=runtime.context.timeout_ms,
            max_limit=runtime.context.max_limit,
            allowed_tables=state.get("allowed_tables") or [],
        )
        ok = result.get("ok") is True
        return {
            "execution_result": result,
            "rows": result.get("rows", []) if ok else [],
            "error": "" if ok else str(result.get("message", "SQL execution failed.")),
            "trace": _trace(
                state,
                "execute_sql",
                ok=ok,
                message=str(result.get("message", "")),
            ),
        } # type: ignore
    except Exception as exc:
        return _error(state, "execute_sql", exc)


async def explain_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        result = await explain_result(
            question=_question_for_model(state),
            sql=state.get("validated_sql", ""),
            rows=state.get("rows", []),
            metrics_result=state.get("metrics_result", {}),
            llm=runtime.context.llm,
        )
        ok = result.get("ok") is True
        return {
            "answer": str(result.get("explanation", "")) if ok else "",
            "error": "" if ok else str(result.get("message", "Explanation failed.")),
            "trace": _trace(
                state,
                "explain",
                ok=ok,
                message=str(result.get("message", "")),
            ),
        } # type: ignore
    except Exception as exc:
        return _error(state, "explain", exc)


async def persist_memory_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    conversation_id = state.get("conversation_id", "")
    user_id = state.get("user_id", "")
    if not conversation_id and not user_id:
        return {
            "trace": _trace(state, "persist_memory", ok=True, message="memory disabled"),
        } # type: ignore
    try:
        await runtime.context.memory_store.save_turn(
            tenant_id=state["tenant_id"],
            conversation_id=conversation_id,
            user_id=user_id,
            question=state["question"],
            contextualized_question=_question_for_model(state),
            sql=state.get("validated_sql", ""),
            rows=state.get("rows", []),
            answer=state.get("answer", ""),
            ok=_state_ok(state),
            error=state.get("error", ""),
            trace=state.get("trace", []),
        )
        return {
            "trace": _trace(state, "persist_memory", ok=True, message="success"),
        } # type: ignore
    except Exception as exc:
        return {
            "trace": _trace(state, "persist_memory", ok=False, message=str(exc)),
        } # type: ignore


async def finalize_node(state: GraphState) -> OutputState:
    intent = state.get("intent")
    sql = state.get("validated_sql", "")
    return {
        "ok": _state_ok(state),
        "question": state["question"],
        "contextualized_question": _question_for_model(state),
        "conversation_id": state.get("conversation_id", ""),
        "user_id": state.get("user_id", ""),
        "tenant_id": state["tenant_id"],
        "intent": asdict(intent) if intent is not None else {},
        "sql": sql,
        "rows": state.get("rows", []),
        "answer": state.get("answer", ""),
        "error": state.get("error", ""),
        "trace": state.get("trace", []),
    }
