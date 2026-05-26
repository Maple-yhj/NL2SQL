from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from agent.react_planner import PlannedAction, choose_action
from agent.react_state import ReactRuntimeConfig, ReactState
from agent.tool_policy import ToolPolicy
from agent.tools.registry import ToolContext, call_tool
from core.stream_chat import GeminiLLM
from engine.intent_parser import parse_intent
from engine.models import QueryIntent


Planner = Callable[..., Awaitable[PlannedAction]]
IntentParser = Callable[..., Awaitable[QueryIntent]]
ToolRunner = Callable[[str, Mapping[str, Any], ToolContext], Awaitable[dict[str, Any]]]


def _build_result(
    state: ReactState,
    *,
    ok: bool,
    message: str,
    trace: list[dict[str, Any]],
) -> dict[str, Any]:
    explanation_result = state.explanation_result or {}
    return {
        "ok": ok,
        "question": state.question,
        "tenant_id": state.config.tenant_id,
        "metrics": state.metrics_result or {},
        "schema": state.schema_result or {},
        "intent": state.intent,
        "sql": state.raw_sql or "",
        "validation": state.validation_result or {},
        "execution": state.execution_result or {},
        "explanation_result": explanation_result,
        "explanation": str(explanation_result.get("explanation", "")),
        "executed_sql": state.validated_sql,
        "rows": state.execution_rows or [],
        "trace": deepcopy(trace),
        "tool_trace": [asdict(item) for item in state.trace],
        "turn_count": len(trace),
        "message": message,
    }


def _new_runtime_config(
    *,
    tenant_id: str,
    execute: bool,
    llm: Any,
    dsn: str | None,
    timeout_ms: int,
    max_limit: int,
    max_steps: int,
) -> ReactRuntimeConfig:
    return ReactRuntimeConfig(
        tenant_id=tenant_id,
        execute_enabled=execute,
        dsn=dsn,
        timeout_ms=timeout_ms,
        max_limit=max_limit,
        max_steps=max_steps,
        llm=llm or GeminiLLM(),
    )


async def run_react_nl2sql(
    question: str,
    tenant_id: str = "demo",
    *,
    execute: bool = False,
    llm: Any = None,
    dsn: str | None = None,
    timeout_ms: int = 10_000,
    max_limit: int = 1000,
    max_steps: int = 8,
    config: ReactRuntimeConfig | None = None,
    policy: ToolPolicy | None = None,
    planner: Planner = choose_action,
    intent_parser: IntentParser = parse_intent,
    tool_runner: ToolRunner = call_tool,
) -> dict[str, Any]:
    if not question or not question.strip():
        raise ValueError("question is empty")

    runtime_config = config or _new_runtime_config(
        tenant_id=tenant_id,
        execute=execute,
        llm=llm,
        dsn=dsn,
        timeout_ms=timeout_ms,
        max_limit=max_limit,
        max_steps=max_steps,
    )
    if not runtime_config.tenant_id or not runtime_config.tenant_id.strip():
        raise ValueError("tenant_id is empty")
    if runtime_config.max_steps <= 0:
        raise ValueError("max_steps must be positive")

    state = ReactState(question=question, config=runtime_config)
    active_policy = policy or ToolPolicy()
    trace: list[dict[str, Any]] = []

    try:
        state.intent = await intent_parser(question, llm=runtime_config.llm)
    except Exception as exc:
        return _build_result(
            state,
            ok=False,
            message=f"Intent parsing failed: {exc}",
            trace=trace,
        )

    for turn in range(1, runtime_config.max_steps + 1):
        if active_policy.can_finish(state):
            return _build_result(state, ok=True, message="success", trace=trace)

        available_tools = active_policy.available_tools(state)
        if not available_tools:
            return _build_result(
                state,
                ok=False,
                message="No available tools before the workflow completed.",
                trace=trace,
            )

        try:
            action = await planner(
                question=question,
                state=state,
                available_tools=available_tools,
                llm=runtime_config.llm,
            )
        except Exception as exc:
            trace.append(
                {
                    "turn": turn,
                    "status": "planner_error",
                    "message": str(exc),
                }
            )
            continue

        decision = active_policy.authorize(
            action.tool_name,
            action.arguments,
            state,
        )
        if not decision.allowed:
            trace.append(
                {
                    "turn": turn,
                    "tool_name": action.tool_name,
                    "arguments": deepcopy(action.arguments),
                    "status": "rejected",
                    "code": decision.code,
                    "message": decision.message,
                }
            )
            continue

        status = "executed"
        try:
            observation = await tool_runner(
                action.tool_name,
                action.arguments,
                state.to_tool_context(),
            )
        except Exception as exc:
            status = "tool_error"
            observation = {
                "ok": False,
                "message": f"Tool execution failed: {exc}",
            }

        state.apply_observation(
            action.tool_name,
            action.arguments,
            observation,
        )
        trace.append(
            {
                "turn": turn,
                "tool_name": action.tool_name,
                "arguments": deepcopy(action.arguments),
                "status": status,
                "ok": observation["ok"],
                "message": str(observation.get("message", "")),
                "observation": deepcopy(observation),
            }
        )

    if active_policy.can_finish(state):
        return _build_result(state, ok=True, message="success", trace=trace)

    return _build_result(
        state,
        ok=False,
        message="The workflow exceeded the maximum steps before completion.",
        trace=trace,
    )
