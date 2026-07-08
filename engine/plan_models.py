from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from engine.models import QueryIntent, coerce_positive_int


class AnalysisType(str, Enum):
    SINGLE_METRIC = "single_metric"
    MULTI_DIMENSIONAL = "multi_dimensional"
    TREND = "trend"
    DETAIL_QUERY = "detail_query"

    @classmethod
    def from_value(cls, value: Any) -> "AnalysisType":
        text = str(value or "").strip().lower()
        aliases = {
            "single": cls.SINGLE_METRIC,
            "single_metric_query": cls.SINGLE_METRIC,
            "multi": cls.MULTI_DIMENSIONAL,
            "multi_dimensional_analysis": cls.MULTI_DIMENSIONAL,
            "trend_analysis": cls.TREND,
            "detail": cls.DETAIL_QUERY,
        }
        if text in aliases:
            return aliases[text]
        return cls(text)


class ExecutionMode(str, Enum):
    FIXED_DAG = "fixed_dag"
    SEMI_DYNAMIC = "semi_dynamic"
    DYNAMIC = "dynamic"


@dataclass(slots=True)
class PlanMetric:
    name: str
    aggregation: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "PlanMetric":
        if isinstance(value, dict):
            return cls(
                name=str(value.get("name") or value.get("metric") or "").strip(),
                aggregation=str(value.get("aggregation") or "").strip(),
            )
        return cls(name=str(value or "").strip())


@dataclass(slots=True)
class PlanDimension:
    name: str
    role: str = "breakdown"

    @classmethod
    def from_value(cls, value: Any) -> "PlanDimension":
        if isinstance(value, dict):
            return cls(
                name=str(value.get("name") or value.get("dimension") or "").strip(),
                role=str(value.get("role") or "breakdown").strip(),
            )
        return cls(name=str(value or "").strip())


@dataclass(slots=True)
class PlanOrder:
    field: str
    direction: str = "desc"

    @classmethod
    def from_value(cls, value: Any) -> "PlanOrder":
        if isinstance(value, dict):
            direction = str(value.get("direction") or "desc").strip().lower()
            return cls(
                field=str(value.get("field") or "").strip(),
                direction="asc" if direction == "asc" else "desc",
            )
        return cls(field=str(value or "").strip())


@dataclass(slots=True)
class PlanResultShape:
    order_by: list[PlanOrder] = field(default_factory=list)
    limit: int | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "PlanResultShape":
        if not isinstance(value, dict):
            return cls()
        return cls(
            order_by=[item for item in (PlanOrder.from_value(v) for v in value.get("order_by", []) or []) if item.field],
            limit=coerce_positive_int(value.get("limit")),
        )


@dataclass(slots=True)
class PlanDSL:
    version: str = "1"
    analysis_type: AnalysisType = AnalysisType.DETAIL_QUERY
    metrics: list[PlanMetric] = field(default_factory=list)
    time_range: dict[str, Any] = field(default_factory=dict)
    dimensions: list[PlanDimension] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    time_grain: str | None = None
    result_shape: PlanResultShape = field(default_factory=PlanResultShape)
    operations: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanDSL":
        analysis_type = AnalysisType.from_value(data.get("analysis_type") or AnalysisType.DETAIL_QUERY.value)
        return cls(
            version=str(data.get("version") or "1"),
            analysis_type=analysis_type,
            metrics=[item for item in (PlanMetric.from_value(v) for v in data.get("metrics", []) or []) if item.name],
            time_range=data.get("time_range") if isinstance(data.get("time_range"), dict) else {},
            dimensions=[
                item
                for item in (PlanDimension.from_value(v) for v in data.get("dimensions", []) or [])
                if item.name
            ],
            filters=_string_list(data.get("filters")),
            time_grain=_clean_optional_string(data.get("time_grain")),
            result_shape=PlanResultShape.from_dict(data.get("result_shape")),
            operations=[
                item for item in data.get("operations", []) or [] if isinstance(item, dict)
            ],
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        )

    @classmethod
    def from_intent(cls, intent: QueryIntent, question: str = "") -> "PlanDSL":
        dimensions = [PlanDimension(name=value, role=_dimension_role(value)) for value in intent.dimensions]
        analysis_type = _classify_analysis_type(question=question, intent=intent, dimensions=dimensions)
        time_grain = _infer_time_grain(question) if analysis_type == AnalysisType.TREND else None
        return cls(
            analysis_type=analysis_type,
            metrics=[PlanMetric(name=value) for value in intent.metrics],
            time_range=dict(intent.time_range),
            dimensions=dimensions,
            filters=list(intent.filters),
            time_grain=time_grain,
            result_shape=PlanResultShape(limit=intent.limit),
        )

    def to_query_intent(self) -> QueryIntent:
        return QueryIntent(
            metrics=[metric.name for metric in self.metrics],
            time_range=dict(self.time_range),
            dimensions=[dimension.name for dimension in self.dimensions],
            filters=list(self.filters),
            limit=self.result_shape.limit,
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["analysis_type"] = self.analysis_type.value
        return value


@dataclass(slots=True)
class ExecutionStep:
    id: str
    tool: str
    depends_on: list[str] = field(default_factory=list)
    enabled: bool = True
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExecutionGraph:
    version: str = "1"
    mode: ExecutionMode = ExecutionMode.FIXED_DAG
    steps: list[ExecutionStep] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode.value,
            "steps": [step.to_dict() for step in self.steps],
            "metadata": dict(self.metadata),
        }


def build_execution_graph(
    plan: PlanDSL,
    *,
    execute: bool,
    mode: ExecutionMode = ExecutionMode.FIXED_DAG,
) -> ExecutionGraph:
    if mode == ExecutionMode.DYNAMIC:
        return _build_dynamic_execution_graph(plan, execute=execute)

    steps = [
        ExecutionStep(
            id="metric_context",
            tool="search_metrics",
            outputs=["metrics_result", "table_names"],
            metadata={"analysis_type": plan.analysis_type.value},
        ),
        ExecutionStep(
            id="schema_context",
            tool="search_schema",
            depends_on=["metric_context"],
            outputs=["schema_result", "allowed_tables"],
        ),
        ExecutionStep(
            id="sql_generation",
            tool="generate_sql",
            depends_on=["metric_context", "schema_context"],
            outputs=["candidate_sql"],
        ),
        ExecutionStep(
            id="sql_validation",
            tool="validate_sql",
            depends_on=["sql_generation"],
            outputs=["validated_sql", "validation_result"],
        ),
    ]
    if execute:
        steps.extend(
            [
                ExecutionStep(
                    id="sql_execution",
                    tool="execute_sql",
                    depends_on=["sql_validation"],
                    outputs=["rows", "execution_result"],
                ),
                ExecutionStep(
                    id="result_explanation",
                    tool="explain",
                    depends_on=["sql_execution"],
                    outputs=["answer"],
                ),
            ]
        )
    return ExecutionGraph(
        mode=ExecutionMode.FIXED_DAG,
        steps=steps,
        metadata={
            "projection": "fixed_dag",
            "future_modes": [ExecutionMode.SEMI_DYNAMIC.value, ExecutionMode.DYNAMIC.value],
        },
    )


def _build_dynamic_execution_graph(plan: PlanDSL, *, execute: bool) -> ExecutionGraph:
    steps = [
        ExecutionStep(
            id="metric_context",
            tool="search_metrics",
            outputs=["metrics_result", "table_names", "domain_context", "domain_constraints"],
            metadata={"analysis_type": plan.analysis_type.value},
        ),
        ExecutionStep(
            id="schema_context",
            tool="search_schema",
            depends_on=["metric_context"],
            outputs=["schema_result", "allowed_tables"],
        ),
        ExecutionStep(
            id="sql_generation",
            tool="generate_sql",
            depends_on=["metric_context", "schema_context"],
            outputs=["candidate_sql"],
        ),
        ExecutionStep(
            id="sql_preparation",
            tool="prepare_sql",
            depends_on=["sql_generation"],
            outputs=["validated_sql", "validation_result", "executable_sql"],
        ),
    ]
    if execute:
        steps.extend(
            [
                ExecutionStep(
                    id="sql_execution",
                    tool="execute_sql",
                    depends_on=["sql_preparation"],
                    outputs=["rows", "execution_result"],
                ),
                ExecutionStep(
                    id="result_explanation",
                    tool="explain_result",
                    depends_on=["sql_execution"],
                    outputs=["answer"],
                ),
            ]
        )
    return ExecutionGraph(
        mode=ExecutionMode.DYNAMIC,
        steps=steps,
        metadata={
            "projection": "dynamic_registry",
            "fallback_mode": ExecutionMode.FIXED_DAG.value,
        },
    )


def format_plan_context(*, plan: PlanDSL, execution_graph: ExecutionGraph) -> str:
    return json.dumps(
        {
            "plan": plan.to_dict(),
            "execution_graph": execution_graph.to_dict(),
        },
        ensure_ascii=True,
        sort_keys=True,
    )


def plan_search_query(*, question: str, plan: PlanDSL | None) -> str:
    if plan is None:
        return question
    parts = [
        question,
        f"analysis_type: {plan.analysis_type.value}",
        "metrics: " + ", ".join(metric.name for metric in plan.metrics),
        "dimensions: " + ", ".join(dimension.name for dimension in plan.dimensions),
        "filters: " + ", ".join(plan.filters),
    ]
    if plan.time_grain:
        parts.append(f"time_grain: {plan.time_grain}")
    if plan.result_shape.limit is not None:
        parts.append(f"limit: {plan.result_shape.limit}")
    return "\n".join(part for part in parts if not part.endswith(": "))


def _classify_analysis_type(
    *,
    question: str,
    intent: QueryIntent,
    dimensions: list[PlanDimension],
) -> AnalysisType:
    lowered = question.lower()
    if _looks_like_trend(lowered, dimensions):
        return AnalysisType.TREND
    if intent.metrics and dimensions:
        return AnalysisType.MULTI_DIMENSIONAL
    if intent.metrics:
        return AnalysisType.SINGLE_METRIC
    return AnalysisType.DETAIL_QUERY


def _looks_like_trend(question: str, dimensions: list[PlanDimension]) -> bool:
    trend_tokens = ("trend", "over time", "by day", "by week", "by month", "timeline")
    if any(token in question for token in trend_tokens):
        return True
    if any(token in question for token in ("\u8d8b\u52bf", "\u8d70\u52bf", "\u53d8\u5316")):
        return True
    return any(dimension.role == "time" for dimension in dimensions)


def _infer_time_grain(question: str) -> str:
    lowered = question.lower()
    if "week" in lowered or "\u5468" in question:
        return "week"
    if "month" in lowered or "\u6708" in question:
        return "month"
    if "quarter" in lowered or "\u5b63\u5ea6" in question:
        return "quarter"
    if "year" in lowered or "\u5e74" in question:
        return "year"
    return "day"


def _dimension_role(name: str) -> str:
    lowered = name.lower()
    if any(token in lowered for token in ("date", "time", "day", "week", "month", "year", "created_at", "paid_at")):
        return "time"
    return "breakdown"


def _clean_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]
