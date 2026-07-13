"""Frozen intermediate representation for the single Data Agent execution graph."""

from __future__ import annotations

from copy import deepcopy
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from data_agent.runtime.models import AgentMode
from data_agent.runtime.composition import stable_digest


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    ),
]
SemanticVersion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=(
            r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
            r"(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
        ),
    ),
]


class GraphModel(BaseModel):
    """Exact-field, immutable base for graph contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    def model_copy(
        self,
        *,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        fields = type(self).model_fields
        unknown = set(update) - set(fields)
        if unknown:
            raise ValueError(
                "model_copy update contains unknown fields: "
                + ", ".join(sorted(unknown))
            )
        values = {
            name: deepcopy(getattr(self, name)) if deep else getattr(self, name)
            for name in fields
        }
        values.update(deepcopy(update) if deep else update)
        return type(self).model_validate(values)


class ArtifactKind(StrEnum):
    RESOLVED_CONTEXT = "resolved_context"
    SEMANTIC_MATCHES = "semantic_matches"
    LOGICAL_PLAN = "logical_plan"
    PLAN_VALIDATION = "plan_validation"
    CATALOG_SNAPSHOT = "catalog_snapshot"
    BOUND_QUERY_PLAN = "bound_query_plan"
    PREPARED_QUERY = "prepared_query"
    STATIC_VALIDATION = "static_validation"
    EXPLAIN_RESULT = "explain_result"
    QUERY_PREVIEW = "query_preview"
    PREVIEW_VALIDATION = "preview_validation"
    QUERY_RESULT = "query_result"
    RESULT_PROFILE = "result_profile"
    RESULT_VALIDATION = "result_validation"
    ANSWER = "answer"
    FINAL = "final"


class NodeKind(StrEnum):
    PURE = "pure"
    PLANNER = "planner"
    VALIDATOR = "validator"
    TOOL = "tool"
    TERMINAL = "terminal"


class EdgeCondition(StrEnum):
    ALWAYS = "always"
    MODE_PLAN = "mode_plan"
    MODE_PREVIEW = "mode_preview"
    MODE_EXECUTE = "mode_execute"
    MODE_NOT_PLAN = "mode_not_plan"


def edge_condition_matches(condition: EdgeCondition, mode: AgentMode) -> bool:
    """Evaluate a normal-success edge over the finite request-mode domain.

    Error outcomes never enter this predicate; they are routed exclusively by
    ``ErrorRouteSpec``. Keeping the two domains disjoint makes their static
    intersections explicit and decidable.
    """

    return {
        EdgeCondition.ALWAYS: True,
        EdgeCondition.MODE_PLAN: mode == AgentMode.PLAN,
        EdgeCondition.MODE_PREVIEW: mode == AgentMode.PREVIEW,
        EdgeCondition.MODE_EXECUTE: mode == AgentMode.EXECUTE,
        EdgeCondition.MODE_NOT_PLAN: mode != AgentMode.PLAN,
    }[condition]


class ApprovalPolicy(StrEnum):
    NONE = "none"
    REQUIRED = "required"


class ErrorCode(StrEnum):
    LOGICAL_PLAN_INVALID = "LOGICAL_PLAN_INVALID"
    BINDING_STALE = "BINDING_STALE"
    SQL_COMPILE_ERROR = "SQL_COMPILE_ERROR"
    SQL_POLICY_VIOLATION = "SQL_POLICY_VIOLATION"
    COST_EXCEEDED = "COST_EXCEEDED"
    EMPTY_RESULT = "EMPTY_RESULT"
    JOIN_EXPLOSION = "JOIN_EXPLOSION"
    ACCESS_DENIED = "ACCESS_DENIED"
    RESULT_SEMANTIC_MISMATCH = "RESULT_SEMANTIC_MISMATCH"


class ErrorBudget(StrEnum):
    CORRECTION = "correction"
    SQL_COMPILE = "sql_compile"
    DIAGNOSTIC = "diagnostic"


class NodeRetryPolicy(GraphModel):
    max_attempts: int = Field(default=1, ge=1, le=5)
    retryable_errors: tuple[ErrorCode, ...] = ()


class ErrorRouteSpec(GraphModel):
    """A statically bounded route that can never widen data authority."""

    code: ErrorCode
    target: Identifier | None = None
    retryable: bool = False
    terminal: bool = False
    max_attempts: int | None = Field(default=None, ge=1, le=3)
    budget: ErrorBudget | None = None
    allowed_modes: tuple[AgentMode, ...] = (
        AgentMode.PLAN,
        AgentMode.PREVIEW,
        AgentMode.EXECUTE,
    )
    may_expand_authority: Literal[False] = False

    @model_validator(mode="after")
    def validate_route(self) -> "ErrorRouteSpec":
        if self.terminal:
            if self.target is not None or self.retryable or self.max_attempts is not None:
                raise ValueError("terminal error route cannot retry or name a target")
        elif self.target is None or not self.retryable or self.max_attempts is None:
            raise ValueError("non-terminal error route must be bounded and retryable")
        if not self.allowed_modes:
            raise ValueError("error route must allow at least one mode")
        return self


class NodeSpec(GraphModel):
    id: Identifier
    kind: NodeKind
    tool_ref: str | None = None
    inputs: tuple[ArtifactKind, ...] = ()
    outputs: tuple[ArtifactKind, ...] = ()
    dependencies: tuple[Identifier, ...] = ()
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    retry_policy: NodeRetryPolicy = Field(default_factory=NodeRetryPolicy)
    on_error: tuple[ErrorRouteSpec, ...] = ()
    approval_policy: ApprovalPolicy = ApprovalPolicy.NONE
    requires_credentials: bool = False

    @model_validator(mode="after")
    def validate_kind(self) -> "NodeSpec":
        if self.kind == NodeKind.TOOL and self.tool_ref is None:
            raise ValueError("tool node requires a tool_ref")
        if self.kind != NodeKind.TOOL and self.tool_ref is not None:
            raise ValueError("only tool nodes may define tool_ref")
        if len(self.inputs) != len(set(self.inputs)):
            raise ValueError("node inputs must be unique")
        if len(self.outputs) != len(set(self.outputs)):
            raise ValueError("node outputs must be unique")
        error_codes = tuple(route.code for route in self.on_error)
        if len(error_codes) != len(set(error_codes)):
            raise ValueError("node error routes must have unique codes")
        return self

    @property
    def input_bindings(self) -> tuple[ArtifactKind, ...]:
        return self.inputs

    @property
    def output_schema(self) -> tuple[ArtifactKind, ...]:
        return self.outputs


class EdgeSpec(GraphModel):
    source: Identifier
    target: Identifier
    condition: EdgeCondition = EdgeCondition.ALWAYS
    artifact: ArtifactKind | None = None


class GraphFragment(GraphModel):
    fragment_id: Identifier
    nodes: tuple[NodeSpec, ...] = ()
    edges: tuple[EdgeSpec, ...] = ()


class BudgetLimits(GraphModel):
    max_correction_rounds: int = Field(default=2, ge=0)
    max_sql_compile_attempts: int = Field(default=3, ge=1)
    max_tool_calls: int = Field(default=24, ge=1)
    max_duration_seconds: int = Field(default=120, ge=1)
    max_result_rows: int = Field(default=1000, ge=1)


class GraphSpec(GraphModel):
    graph_id: Identifier
    version: SemanticVersion
    entry_node: Identifier
    terminal_nodes: tuple[Identifier, ...] = Field(min_length=1)
    nodes: tuple[NodeSpec, ...] = Field(min_length=1)
    edges: tuple[EdgeSpec, ...]
    limits: BudgetLimits
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def validate_graph_digest(self) -> "GraphSpec":
        expected = stable_digest(
            {
                "graph_id": self.graph_id,
                "version": self.version,
                "entry_node": self.entry_node,
                "terminal_nodes": self.terminal_nodes,
                "nodes": self.nodes,
                "edges": self.edges,
                "limits": self.limits,
            }
        )
        if self.digest != expected:
            raise ValueError("graph digest does not match the frozen graph spec")
        return self

    def node(self, node_id: str) -> NodeSpec | None:
        return next((node for node in self.nodes if node.id == node_id), None)


PLATFORM_BUDGET_CEILING = BudgetLimits()
