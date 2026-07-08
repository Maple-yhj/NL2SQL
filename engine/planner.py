from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.structured_output import extract_json_object
from engine.models import QueryIntent
from engine.plan_models import (
    ExecutionMode,
    ExecutionGraph,
    PlanDSL,
    build_execution_graph,
)

if TYPE_CHECKING:
    from core.llm import LLMProtocol
else:
    LLMProtocol = Any


PLANNER_SYSTEM = """
You are a BI planning layer for an NL2SQL engine.
Return only JSON. Do not generate SQL.
Your output is Plan DSL, not executable database logic.

Supported analysis_type values:
- single_metric: one or more metrics without dimensional breakdown
- multi_dimensional: metrics grouped by one or more business dimensions
- trend: metrics grouped by a time dimension and time_grain
- detail_query: row-level listing or record lookup

JSON shape:
{
  "version": "1",
  "analysis_type": "single_metric | multi_dimensional | trend | detail_query",
  "metrics": [{"name": "metric name", "aggregation": "optional"}],
  "time_range": {"start": "YYYY-MM-DD or empty", "end": "YYYY-MM-DD or empty"},
  "dimensions": [{"name": "dimension name", "role": "breakdown | time"}],
  "filters": ["business filters"],
  "time_grain": "day | week | month | quarter | year | null",
  "result_shape": {"order_by": [{"field": "name", "direction": "asc | desc"}], "limit": null},
  "operations": [],
  "metadata": {}
}
If Parsed intent includes a positive limit, preserve it in result_shape.limit.
""".strip()


@dataclass(slots=True)
class PlanBundle:
    plan: PlanDSL
    execution_graph: ExecutionGraph
    intent: QueryIntent
    ok: bool
    message: str = ""


async def plan_query(
    *,
    question: str,
    intent: QueryIntent | None,
    llm: LLMProtocol,
    execute: bool,
    execution_mode: ExecutionMode = ExecutionMode.FIXED_DAG,
) -> PlanBundle:
    base_intent = intent or QueryIntent()
    try:
        raw = await llm.complete(
            prompt=_build_planner_prompt(question=question, intent=base_intent),
            system=PLANNER_SYSTEM,
            max_output_tokens=1536,
        )
        plan = PlanDSL.from_dict(extract_json_object(raw))
        if base_intent.limit is not None:
            plan.result_shape.limit = base_intent.limit
        message = "success"
        ok = True
    except Exception as exc:
        plan = PlanDSL.from_intent(base_intent, question=question)
        message = f"planner fallback: {exc}"
        ok = False

    execution_graph = build_execution_graph(plan, execute=execute, mode=execution_mode)
    return PlanBundle(
        plan=plan,
        execution_graph=execution_graph,
        intent=plan.to_query_intent(),
        ok=ok,
        message=message,
    )


def _build_planner_prompt(*, question: str, intent: QueryIntent) -> str:
    return f"""
Question:
{question}

Parsed intent:
metrics: {intent.metrics}
time_range: {intent.time_range}
dimensions: {intent.dimensions}
filters: {intent.filters}
limit: {intent.limit}
""".strip()
