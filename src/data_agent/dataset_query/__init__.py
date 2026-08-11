"""Reusable deterministic services for governed user-dataset queries."""

from importlib import import_module

from .contracts import (
    AnalysisType,
    LogicalQueryPlan,
    PreparedQuery,
    QueryParameter,
    RelationshipRouteEvidence,
    ResultShape,
)
from .models import (
    DatasetAggregation,
    DatasetConversationContext,
    DatasetFilter,
    DatasetFilterOperator,
    DatasetOrdering,
    DatasetPlanPatch,
    DatasetPlanningResult,
    DatasetPlanStatus,
    DatasetPlanUpdate,
    DatasetQueryPlan,
)
from .program import (
    DatasetAggregateExpression,
    DatasetBinaryExpression,
    DatasetFieldExpression,
    DatasetFunctionExpression,
    DatasetJoinSource,
    DatasetLiteralExpression,
    DatasetMetricExpression,
    DatasetOutputExpression,
    DatasetProgramOrdering,
    DatasetProjection,
    DatasetQueryProgram,
    DatasetQueryStage,
    DatasetRootSource,
    DatasetStageJoinCondition,
    DatasetStageSource,
    DatasetUnaryExpression,
    DatasetUnionStage,
)


_LAZY_EXPORTS = {
    "DatasetQueryCompiler": (".compiler", "DatasetQueryCompiler"),
    "DatasetExecutionAuthority": (".executor", "DatasetExecutionAuthority"),
    "DatasetQueryExecutor": (".executor", "DatasetQueryExecutor"),
    "DatasetLogicalPlanner": (".planner", "DatasetLogicalPlanner"),
    "DatasetProgramPlanningResult": (
        ".program_planner",
        "DatasetProgramPlanningResult",
    ),
    "DatasetQueryProgramPlanner": (
        ".program_planner",
        "DatasetQueryProgramPlanner",
    ),
    "DatasetQueryProgramCompiler": (".program_compiler", "DatasetQueryProgramCompiler"),
    "answer_for_result": (".results", "answer_for_result"),
    "chart_for_result": (".results", "chart_for_result"),
    "json_value": (".results", "json_value"),
    "tabular_rows": (".results", "tabular_rows"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


__all__ = [
    "AnalysisType",
    "DatasetAggregation",
    "DatasetAggregateExpression",
    "DatasetBinaryExpression",
    "DatasetConversationContext",
    "DatasetExecutionAuthority",
    "DatasetFilter",
    "DatasetFilterOperator",
    "DatasetFieldExpression",
    "DatasetFunctionExpression",
    "DatasetLogicalPlanner",
    "DatasetJoinSource",
    "DatasetLiteralExpression",
    "DatasetMetricExpression",
    "DatasetOutputExpression",
    "DatasetOrdering",
    "DatasetPlanPatch",
    "DatasetPlanningResult",
    "DatasetPlanStatus",
    "DatasetPlanUpdate",
    "DatasetProgramPlanningResult",
    "DatasetProgramOrdering",
    "DatasetProjection",
    "DatasetQueryProgram",
    "DatasetQueryProgramCompiler",
    "DatasetQueryProgramPlanner",
    "DatasetQueryStage",
    "DatasetQueryCompiler",
    "DatasetQueryExecutor",
    "DatasetQueryPlan",
    "DatasetRootSource",
    "DatasetStageJoinCondition",
    "DatasetStageSource",
    "DatasetUnaryExpression",
    "DatasetUnionStage",
    "LogicalQueryPlan",
    "PreparedQuery",
    "QueryParameter",
    "RelationshipRouteEvidence",
    "ResultShape",
    "answer_for_result",
    "chart_for_result",
    "json_value",
    "tabular_rows",
]
