from __future__ import annotations

from time import perf_counter
from typing import Any

from graph.tools.policy import evaluate_pre_call_policy
from graph.tools.registry import ToolRegistry
from graph.tools.tracing import build_tool_trace_event, summarize_tool_payload, utc_now_iso


SQL_GENERATION_TOOLS = {"generate_sql"}
SQL_VALIDATION_TOOLS = {"prepare_sql", "validate_sql"}


async def execute_dynamic_graph(
    state: dict[str, Any],
    runtime: Any,
    *,
    registry: ToolRegistry,
    max_steps: int = 50,
) -> dict[str, Any]:
    working = dict(state)
    ordered = _topological_steps(_enabled_steps(_execution_steps(working)))
    if isinstance(ordered, dict):
        return _fail(working, "dynamic_execute", str(ordered["error"]))

    generation_index = _first_tool_index(ordered, SQL_GENERATION_TOOLS, registry)
    validation_index = _first_tool_index(ordered, SQL_VALIDATION_TOOLS, registry)
    if generation_index is not None and validation_index is not None and generation_index < validation_index:
        return await _execute_with_sql_retry(
            working,
            runtime,
            registry=registry,
            ordered_steps=ordered,
            generation_index=generation_index,
            validation_index=validation_index,
            max_steps=max_steps,
        )

    executed = 0
    for step in ordered:
        if executed >= max_steps:
            return _fail(working, "dynamic_execute", f"Dynamic execution exceeded {max_steps} steps.")
        working = await _execute_step(working, runtime, registry, step)
        executed += 1
        if working.get("error"):
            break
    return working


async def _execute_with_sql_retry(
    working: dict[str, Any],
    runtime: Any,
    *,
    registry: ToolRegistry,
    ordered_steps: list[dict[str, Any]],
    generation_index: int,
    validation_index: int,
    max_steps: int,
) -> dict[str, Any]:
    executed = 0
    max_attempts = int(getattr(runtime.context, "max_validation_attempts", 2))
    prefix = ordered_steps[:generation_index]
    cycle = ordered_steps[generation_index : validation_index + 1]
    suffix = ordered_steps[validation_index + 1 :]

    for step in prefix:
        if executed >= max_steps:
            return _fail(working, "dynamic_execute", f"Dynamic execution exceeded {max_steps} steps.")
        working = await _execute_step(working, runtime, registry, step)
        executed += 1
        if working.get("error"):
            return working

    while True:
        for step in cycle:
            if executed >= max_steps:
                return _fail(working, "dynamic_execute", f"Dynamic execution exceeded {max_steps} steps.")
            if _canonical_tool_name(registry, str(step.get("tool") or "")) in SQL_GENERATION_TOOLS:
                working["error"] = ""
            working = await _execute_step(working, runtime, registry, step)
            executed += 1
            if (
                working.get("error")
                and _canonical_tool_name(registry, str(step.get("tool") or "")) not in SQL_VALIDATION_TOOLS
            ):
                return working

        validation = working.get("validation_result") or {}
        if validation.get("ok") is True:
            working["error"] = ""
            break
        if int(working.get("validation_attempts", 0)) >= max_attempts:
            return working

    for step in suffix:
        if executed >= max_steps:
            return _fail(working, "dynamic_execute", f"Dynamic execution exceeded {max_steps} steps.")
        working = await _execute_step(working, runtime, registry, step)
        executed += 1
        if working.get("error"):
            break
    return working


async def _execute_step(
    working: dict[str, Any],
    runtime: Any,
    registry: ToolRegistry,
    step: dict[str, Any],
) -> dict[str, Any]:
    tool_name = str(step.get("tool") or "").strip()
    try:
        spec = registry.get(tool_name)
    except KeyError as exc:
        message = str(exc.args[0] if exc.args else exc)
        return _fail(working, f"dynamic:{tool_name or 'unknown'}", message)
    canonical_name = spec.name
    inputs = _step_inputs(step)
    started_at = utc_now_iso()
    started = perf_counter()
    input_summary = summarize_tool_payload(tool_name, {**working, **inputs})
    decision = evaluate_pre_call_policy(
        spec=spec,
        state=working,
        runtime=runtime,
        inputs=inputs,
    )
    if not decision.allowed:
        result = decision.to_tool_result()
        message = result.error.message if result.error else "Tool policy failed."
        duration_ms = (perf_counter() - started) * 1000
        return {
            **working,
            "error": message,
            "tool_trace": [
                *(working.get("tool_trace", []) or []),
                build_tool_trace_event(
                    tool_name=tool_name,
                    canonical_name=canonical_name,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    ok=False,
                    error_code=result.error.code if result.error else "tool_policy_failed",
                    input_summary=input_summary,
                    output_summary={},
                    retry_count=int(working.get("validation_attempts", 0)),
                ),
            ],
            "trace": [
                *(working.get("trace", []) or []),
                {
                    "node": f"dynamic:{canonical_name}",
                    "ok": False,
                    "message": message,
                    "policy_ok": False,
                    "blocked_reason": result.error.code if result.error else "tool_policy_failed",
                },
            ],
        }
    working["_tool_call_count"] = int(working.get("_tool_call_count", 0)) + 1

    before_trace_len = len(working.get("trace", []) or [])
    try:
        update = await spec.handler(working, runtime, inputs)
    except Exception as exc:
        duration_ms = (perf_counter() - started) * 1000
        failed = _fail(working, f"dynamic:{canonical_name}", str(exc))
        failed["tool_trace"] = [
            *(working.get("tool_trace", []) or []),
            build_tool_trace_event(
                tool_name=tool_name,
                canonical_name=canonical_name,
                started_at=started_at,
                duration_ms=duration_ms,
                ok=False,
                error_code="tool_exception",
                input_summary=input_summary,
                output_summary={},
                retry_count=int(working.get("validation_attempts", 0)),
            ),
        ]
        return failed

    working.update(update or {})
    duration_ms = (perf_counter() - started) * 1000
    working["tool_trace"] = [
        *(working.get("tool_trace", []) or []),
        build_tool_trace_event(
            tool_name=tool_name,
            canonical_name=canonical_name,
            started_at=started_at,
            duration_ms=duration_ms,
            ok=not bool(working.get("error")),
            error_code="" if not working.get("error") else "tool_error",
            input_summary=input_summary,
            output_summary=summarize_tool_payload(canonical_name, update or {}),
            retry_count=int(working.get("validation_attempts", 0)),
        ),
    ]
    after_trace = working.get("trace", []) or []
    if len(after_trace) == before_trace_len:
        working["trace"] = [
            *after_trace,
            {"node": f"dynamic:{canonical_name}", "ok": True, "message": "success"},
        ]
    return working


def _execution_steps(state: dict[str, Any]) -> list[dict[str, Any]]:
    graph = state.get("execution_graph") if isinstance(state.get("execution_graph"), dict) else {}
    steps = graph.get("steps") if isinstance(graph.get("steps"), list) else []
    return [step for step in steps if isinstance(step, dict)]


def _enabled_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in steps if step.get("enabled", True)]


def _topological_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]] | dict[str, str]:
    by_id = {_step_id(step): step for step in steps}
    if len(by_id) != len(steps):
        return {"error": "Dynamic execution graph contains duplicate or empty step ids."}
    ordered: list[dict[str, Any]] = []
    completed: set[str] = set()
    pending = dict(by_id)
    while pending:
        progressed = False
        for step_id, step in list(pending.items()):
            depends_on = {str(value) for value in step.get("depends_on", []) or []}
            missing = depends_on - set(by_id)
            if missing:
                return {"error": f"Step {step_id} depends on unknown step(s): {', '.join(sorted(missing))}"}
            if depends_on <= completed:
                ordered.append(step)
                completed.add(step_id)
                del pending[step_id]
                progressed = True
        if not progressed:
            return {"error": "Dynamic execution graph contains a dependency cycle."}
    return ordered


def _step_id(step: dict[str, Any]) -> str:
    return str(step.get("id") or "").strip()


def _step_inputs(step: dict[str, Any]) -> dict[str, Any]:
    value = step.get("inputs")
    return dict(value) if isinstance(value, dict) else {}


def _first_tool_index(
    steps: list[dict[str, Any]],
    tool_names: set[str],
    registry: ToolRegistry | None = None,
) -> int | None:
    for index, step in enumerate(steps):
        tool = _canonical_tool_name(registry, str(step.get("tool") or ""))
        if tool in tool_names:
            return index
    return None


def _canonical_tool_name(registry: ToolRegistry | None, tool_name: str) -> str:
    if registry is None:
        return tool_name
    try:
        return registry.canonical_name(tool_name)
    except KeyError:
        return tool_name


def _fail(state: dict[str, Any], node_name: str, message: str) -> dict[str, Any]:
    return {
        **state,
        "error": message,
        "trace": [
            *(state.get("trace", []) or []),
            {"node": node_name, "ok": False, "message": message},
        ],
    }
