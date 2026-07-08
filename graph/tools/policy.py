from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from graph.tools.contracts import ToolError, ToolResult, ToolSpec


class ToolPolicyViolation(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retry_hint: str | None = None
    recoverable: bool = False


class ToolPolicyDecision(BaseModel):
    allowed: bool
    violations: list[ToolPolicyViolation] = Field(default_factory=list)

    def to_tool_result(self) -> ToolResult:
        if self.allowed:
            return ToolResult(ok=True, summary="Tool policy passed.")
        first = self.violations[0]
        return ToolResult(
            ok=False,
            error=ToolError(
                code=first.code,
                message=first.message,
                recoverable=first.recoverable,
                retry_hint=first.retry_hint,
            ),
        )


def evaluate_pre_call_policy(
    *,
    spec: ToolSpec,
    state: dict[str, Any],
    runtime: Any,
    inputs: dict[str, Any] | None = None,
) -> ToolPolicyDecision:
    context = getattr(runtime, "context", None)
    inputs = inputs or {}
    violations: list[ToolPolicyViolation] = []

    allowed_risk_levels = tuple(
        getattr(context, "allowed_tool_risk_levels", ("low", "medium")) or ()
    )
    if spec.risk_level not in allowed_risk_levels:
        violations.append(
            ToolPolicyViolation(
                code="tool_risk_not_allowed",
                message=f"Tool risk level '{spec.risk_level}' is not allowed for this runtime.",
                retry_hint="Use a lower-risk tool or explicitly allow this risk level in GraphContext.",
            )
        )

    if _requires_tenant_id(spec) and not str(state.get("tenant_id") or inputs.get("tenant_id") or "").strip():
        violations.append(
            ToolPolicyViolation(
                code="missing_tenant_id",
                message=f"Tool '{spec.name}' requires tenant_id.",
                retry_hint="Provide tenant_id before invoking the tool.",
                recoverable=True,
            )
        )

    max_tool_calls = getattr(context, "max_tool_calls", None)
    if max_tool_calls is not None and int(state.get("_tool_call_count", 0)) >= int(max_tool_calls):
        violations.append(
            ToolPolicyViolation(
                code="tool_call_budget_exceeded",
                message=f"Tool call budget exceeded: max_tool_calls={max_tool_calls}.",
            )
        )

    if bool(getattr(context, "read_only_tools", False)) and spec.side_effects == "write":
        violations.append(
            ToolPolicyViolation(
                code="tool_write_blocked",
                message=f"Tool '{spec.name}' has write side effects and read-only mode is enabled.",
            )
        )

    return ToolPolicyDecision(allowed=not violations, violations=violations)


def _requires_tenant_id(spec: ToolSpec) -> bool:
    if "tenant_id" in spec.input_keys:
        return True
    input_schema = spec.input_schema
    if input_schema is None:
        return False
    return "tenant_id" in getattr(input_schema, "model_fields", {})
