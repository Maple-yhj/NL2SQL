# agent/tool_policy.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.react_state import ReactState
from agent.tools.registry import ToolSpec, get_tool_spec


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    tool_name: str
    code: str
    message: str


class ToolPolicy:
    def available_tools(self, state: ReactState) -> list[ToolSpec]:
        """
        返回当前可暴露给 planner 的工具定义。
        只返回满足前置条件且仍有调用预算的工具。
        """
        allowed_names = []

        if self._may_search_metrics(state):
            allowed_names.append("search_metrics")

        if self._may_search_schema(state):
            allowed_names.append("search_schema")

        if self._may_generate_sql(state):
            allowed_names.append("generate_sql")

        if self._may_validate_sql(state):
            allowed_names.append("validate_sql")

        if self._may_execute_sql(state):
            allowed_names.append("execute_sql")

        if self._may_explain_result(state):
            allowed_names.append("explain_result")

        return [get_tool_spec(name) for name in allowed_names]

    def authorize(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        state: ReactState,
    ) -> PolicyDecision:
        """
        对 planner 已选出的 action 再做一次执行前判定。
        """
        try:
            get_tool_spec(tool_name)
        except KeyError:
            return PolicyDecision(
                allowed=False,
                tool_name=tool_name,
                code="unknown_tool",
                message=f"Tool is not registered: {tool_name}",
            )
    
        if not self._has_budget(tool_name, state):
            return PolicyDecision(
                allowed=False,
                tool_name=tool_name,
                code="max_calls_exceeded",
                message=f"Tool call limit reached: {tool_name}",
            )
    
        allowed_names = {spec.name for spec in self.available_tools(state)}
    
        if tool_name not in allowed_names:
            return PolicyDecision(
                allowed=False,
                tool_name=tool_name,
                code="prerequisite_not_met",
                message=f"Tool is not available in the current state: {tool_name}",
            )
    
        return PolicyDecision(
            allowed=True,
            tool_name=tool_name,
            code="allowed",
            message="Tool call allowed.",
        )

    def can_finish(self, state: ReactState) -> bool:
        if state.config.execute_enabled:
            return bool(
                state.explanation_result
                and state.explanation_result.get("ok") is True
            )

        return bool(state.validated_sql)

    def _has_budget(self, tool_name: str, state: ReactState) -> bool:
        spec = get_tool_spec(tool_name)
        used_calls = state.tool_call_counts.get(tool_name, 0)
        return used_calls < spec.max_calls

    def _may_search_metrics(self, state: ReactState) -> bool:
        return (
            self._has_budget("search_metrics", state)
            and not self._succeeded(state.metrics_result)
        )


    def _may_search_schema(self, state: ReactState) -> bool:
        return (
            self._has_budget("search_schema", state)
            and self._succeeded(state.metrics_result)
            and not state.allowed_tables
        )


    def _may_generate_sql(self, state: ReactState) -> bool:
        validation_failed = bool(
            state.validation_result
            and state.validation_result.get("ok") is False
        )
        return (
            self._has_budget("generate_sql", state)
            and state.intent is not None
            and self._succeeded(state.schema_result)
            and bool(state.allowed_tables)
            and (not state.raw_sql or validation_failed)
        )


    def _may_validate_sql(self, state: ReactState) -> bool:
        return (
            self._has_budget("validate_sql", state)
            and self._succeeded(state.schema_result)
            and bool(state.allowed_tables)
            and bool(state.raw_sql)
            and state.validation_result is None
        )


    def _may_execute_sql(self, state: ReactState) -> bool:
        return (
            self._has_budget("execute_sql", state)
            and state.config.execute_enabled
            and bool(state.validated_sql)
        )


    def _may_explain_result(self, state: ReactState) -> bool:
        return (
            self._has_budget("explain_result", state)
            and state.execution_rows is not None
        )


    @staticmethod
    def _succeeded(result: dict[str, Any] | None) -> bool:
        return bool(result and result.get("ok") is True)
