from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from langgraph.runtime import Runtime

from catalog.domain_loader import try_load_domain_profile
from catalog.domain_resolver import format_domain_context, resolve_domain_context
from engine.intent_parser import parse_intent
from engine.models import coerce_positive_int
from engine.plan_models import (
    ExecutionMode,
    PlanDSL,
    format_plan_context,
    plan_search_query,
)
from engine.planner import plan_query
from graph.dynamic_executor import execute_dynamic_graph
from graph.context import GraphContext
from graph.data_memory import data_memory_to_dict, extract_pending_memory_updates
from graph.message_type import classify_message_type
from graph.state import GraphState, OutputState
from graph.tools.contextualize_question import contextualize_question
from graph.tools.execute_sql import execute_sql
from graph.tools.explain_result import explain_result
from graph.tools.explain_table_result import explain_table_result
from graph.tools.domain_sql_validator import validate_domain_sql
from graph.tools.sql_generator import generate_sql
from graph.tools.sql_store import search_metrics, search_schema
from graph.tools.tenant_scope import apply_tenant_scope
from graph.tools.runtime_registry import build_runtime_tool_registry
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


def _plan_from_state(state: GraphState) -> PlanDSL | None:
    value = state.get("plan")
    if not isinstance(value, dict):
        return None
    try:
        return PlanDSL.from_dict(value)
    except Exception:
        return None


def _effective_max_limit(state: GraphState, configured_max_limit: int) -> int:
    intent_limit = _intent_limit_from_state(state)
    if intent_limit is None:
        plan = _plan_from_state(state)
        intent_limit = plan.result_shape.limit if plan is not None else None
    if intent_limit is None:
        return configured_max_limit
    return min(intent_limit, configured_max_limit)


def _intent_limit_from_state(state: GraphState) -> int | None:
    for key in ("planned_intent", "intent"):
        intent = state.get(key)
        if isinstance(intent, dict):
            limit = coerce_positive_int(intent.get("limit"))
        else:
            limit = coerce_positive_int(getattr(intent, "limit", None))
        if limit is not None:
            return limit
    return None


def _query_for_retrieval(state: GraphState) -> str:
    return plan_search_query(
        question=_question_for_model(state),
        plan=_plan_from_state(state),
    )


def _state_ok(state: GraphState) -> bool:
    sql = state.get("validated_sql", "")
    error = state.get("error", "")
    ok = bool(sql) and not error
    if state.get("execute", False):
        ok = ok and (state.get("execution_result") or {}).get("ok") is True
    return ok


def _message_type(state: GraphState) -> str:
    return classify_message_type(
        question=state.get("question", ""),
        contextualized_question=_question_for_model(state),
        sql=state.get("validated_sql", ""),
        rows=state.get("rows", []),
        error=state.get("error", ""),
    )


def _intent_output(intent: Any) -> dict[str, Any]:
    if intent is None:
        return {}
    value = dict(intent) if isinstance(intent, dict) else asdict(intent)
    if value.get("limit") is None:
        value.pop("limit", None)
    return value


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


def _domain_context_for_state(
    state: GraphState,
    metrics_result: dict[str, Any],
) -> tuple[list[str], str, dict[str, Any]]:
    profile = try_load_domain_profile()
    if profile is None:
        return [], "", {}
    resolution = resolve_domain_context(
        profile=profile,
        question=_question_for_model(state),
        intent=state.get("planned_intent") or state.get("intent"),
        plan=_plan_from_state(state),
        metrics_result=metrics_result,
    )
    schema_tables = _merge_table_names(resolution.required_tables, resolution.optional_tables)
    return schema_tables, format_domain_context(resolution), asdict(resolution)


def _merge_table_names(*groups: list[str]) -> list[str]:
    names: list[str] = []
    for group in groups:
        for name in group:
            if name and name not in names:
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
        "data_memories": [],
        "pending_memory_updates": [],
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


async def recall_data_memory_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        memories = await runtime.context.data_memory_store.search(
            tenant_id=state["tenant_id"],
            user_id=state.get("user_id", ""),
            conversation_id=state.get("conversation_id", ""),
            query=_question_for_model(state),
            limit=runtime.context.data_memory_recall_limit,
        )
        data_memories = [data_memory_to_dict(memory) for memory in memories]
        return {
            "data_memories": data_memories,
            "trace": _trace(
                state,
                "recall_data_memory",
                ok=True,
                message=f"loaded {len(data_memories)} data memory item(s)",
            ),
        } # type: ignore
    except Exception as exc:
        return {
            "data_memories": [],
            "trace": _trace(state, "recall_data_memory", ok=False, message=str(exc)),
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


async def plan_query_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        bundle = await plan_query(
            question=_question_for_model(state),
            intent=state.get("intent"),
            llm=runtime.context.llm,
            execute=state.get("execute", False),
            execution_mode=(
                ExecutionMode.DYNAMIC
                if runtime.context.agent_mode == "dynamic"
                else ExecutionMode.FIXED_DAG
            ),
        )
        plan = bundle.plan
        execution_graph = bundle.execution_graph
        return {
            "plan": plan.to_dict(),
            "execution_graph": execution_graph.to_dict(),
            "planned_intent": bundle.intent,
            "plan_context": format_plan_context(
                plan=plan,
                execution_graph=execution_graph,
            ),
            "trace": _trace(
                state,
                "plan_query",
                ok=bundle.ok,
                message=bundle.message,
            ),
        } # type: ignore
    except Exception as exc:
        return _error(state, "plan_query", exc)


async def search_metrics_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        result = await search_metrics(
            query=_query_for_retrieval(state),
            tenant_id=state["tenant_id"],
            embedding_client=runtime.context.embeddings,
        )
        metric_tables = _metric_table_names(result)
        domain_tables, domain_context, domain_constraints = _domain_context_for_state(state, result)
        return {
            "metrics_result": result,
            "table_names": _merge_table_names(metric_tables, domain_tables),
            "domain_context": domain_context,
            "domain_constraints": domain_constraints,
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
            query=_query_for_retrieval(state),
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
            intent=state.get("planned_intent") or state.get("intent"),
            metrics_result=state.get("metrics_result", {}),
            schema_result=state.get("schema_result", {}),
            retry_feedback=state.get("retry_feedback"),
            tenant_id=state["tenant_id"],
            domain_context=state.get("domain_context", ""),
            conversation_history=state.get("conversation_history", []),
            user_memories=state.get("user_memories", []),
            data_memories=state.get("data_memories", []),
            plan_context=state.get("plan_context"),
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
            max_limit=_effective_max_limit(state, runtime.context.max_limit),
        )
        if result.get("ok") is True:
            domain_result = validate_domain_sql(
                sql=str(result.get("normalized_sql", "")),
                constraints=state.get("domain_constraints"),
            )
            if domain_result.get("ok") is not True:
                result = {
                    **result,
                    "ok": False,
                    "message": str(domain_result.get("message") or "SQL domain validation failed."),
                    "violations": [
                        *(result.get("violations", []) or []),
                        *(domain_result.get("violations", []) or []),
                    ],
                    "warnings": [
                        *(result.get("warnings", []) or []),
                        *(domain_result.get("warnings", []) or []),
                    ],
                    "domain_validation": domain_result,
                }
        if result.get("ok") is True:
            logical_sql = str(result.get("normalized_sql", ""))
            executable_sql = apply_tenant_scope(logical_sql, tenant_id=state["tenant_id"])
            result = {
                **result,
                "logical_sql": logical_sql,
                "tenant_scoped_sql": executable_sql,
                "executable_sql": executable_sql,
                "normalized_sql": executable_sql,
            }
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


async def dynamic_execute_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        return await execute_dynamic_graph(
            state,
            runtime,
            registry=build_runtime_tool_registry(
                {
                    "search_metrics": lambda state, runtime, inputs: search_metrics_node(state, runtime),
                    "search_schema": lambda state, runtime, inputs: search_schema_node(state, runtime),
                    "generate_sql": lambda state, runtime, inputs: generate_sql_node(state, runtime),
                    "prepare_sql": lambda state, runtime, inputs: validate_sql_node(state, runtime),
                    "validate_sql": lambda state, runtime, inputs: validate_sql_node(state, runtime),
                    "execute_sql": lambda state, runtime, inputs: execute_sql_node(state, runtime),
                    "explain_result": lambda state, runtime, inputs: explain_node(state, runtime),
                    "explain_table_result": lambda state, runtime, inputs: explain_node(state, runtime),
                }
            ),
        ) # type: ignore
    except Exception as exc:
        return _error(state, "dynamic_execute", exc)


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
            max_limit=_effective_max_limit(state, runtime.context.max_limit),
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
        explainer = explain_table_result if _message_type(state) == "table" else explain_result
        result = await explainer(
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
            message_type=_message_type(state),
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


async def propose_memory_updates_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        updates = extract_pending_memory_updates(
            question=state.get("question", ""),
            contextualized_question=_question_for_model(state),
            sql=state.get("validated_sql", ""),
            answer=state.get("answer", ""),
            error=state.get("error", ""),
        )
        return {
            "pending_memory_updates": updates,
            "trace": _trace(
                state,
                "propose_memory_updates",
                ok=True,
                message=f"proposed {len(updates)} memory update(s)",
            ),
        } # type: ignore
    except Exception as exc:
        return {
            "pending_memory_updates": [],
            "trace": _trace(state, "propose_memory_updates", ok=False, message=str(exc)),
        } # type: ignore


async def finalize_node(state: GraphState) -> OutputState:
    intent = state.get("planned_intent") or state.get("intent")
    sql = state.get("validated_sql", "")
    output: OutputState = {
        "ok": _state_ok(state),
        "question": state["question"],
        "contextualized_question": _question_for_model(state),
        "conversation_id": state.get("conversation_id", ""),
        "user_id": state.get("user_id", ""),
        "tenant_id": state["tenant_id"],
        "intent": _intent_output(intent),
        "sql": sql,
        "message_type": _message_type(state),
        "rows": state.get("rows", []),
        "answer": state.get("answer", ""),
        "error": state.get("error", ""),
        "trace": state.get("trace", []),
        "pending_memory_updates": state.get("pending_memory_updates", []),
    }
    if "tool_trace" in state:
        output["tool_trace"] = state.get("tool_trace", [])
    return output
