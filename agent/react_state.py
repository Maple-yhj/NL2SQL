from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from pydantic import Field
from tools.registry import ToolContext
from copy import deepcopy
import re


_SUPPORTED_TOOLS = {
    "search_metrics",
    "search_schema",
    "validate_sql",
    "execute_sql",
    "explain_result",
}

_JOIN_TABLE_RE = re.compile(
    r"\bJOIN\s+([A-Za-z_][A-Za-z0-9_\.]*)",
    re.IGNORECASE,
)

def _extract_metric_table_names(metrics_result: dict[str, Any]) -> list[str]:
    table_names: list[str] = []
    for metric in metrics_result.get("metrics", []) or []:
        base_table = str(metric.get("base_table") or "").strip()
        if base_table:
            table_name = base_table.split()[0].split(".")[-1]
            if table_name not in table_names:
                table_names.append(table_name)
        for join_clause in metric.get("join_tables", []) or []:
            for match in _JOIN_TABLE_RE.finditer(str(join_clause)):
                table_name = match.group(1).split(".")[-1]
                if table_name not in table_names:
                    table_names.append(table_name)
    return table_names


def _extract_allowed_tables(schema_result: dict[str, Any]) -> list[str]:
    allowed_tables: list[str] = []

    for table in schema_result.get("schema", []) or []:
        table_name = table.get("table_name")
        if table_name and table_name not in allowed_tables:
            allowed_tables.append(table_name)

    return allowed_tables

@dataclass(frozen=True)
class ReactRuntimeConfig:
    tenant_id: str
    execute_enabled: bool = False
    dsn: str | None = None
    timeout_ms: int = 10_000
    max_limit: int = 1000
    max_steps: int = 8
    llm: Any = None

@dataclass
class ToolTraceItem:
    step: int
    tool_name: str
    arguments: dict[str, Any]
    ok: bool
    message: str
    observation: dict[str, Any]

@dataclass
class ReactState:
    question: str
    config: ReactRuntimeConfig

    intent: dict[str, Any] | None = None
    metrics_result: dict[str, Any] | None = None
    schema_result: dict[str, Any] | None = None

    raw_sql: str = ""
    validation_result: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None
    explanation_result: dict[str, Any] | None = None

    table_names: list[str] = Field(default_factory=list)
    allowed_tables: list[str] = Field(default_factory=list)

    step_count: int = 0
    tool_call_counts: dict[str, int] = Field(default_factory=dict)
    trace: list["ToolTraceItem"] = Field(default_factory=list)


    @property
    def validated_sql(self) -> str:
        if not self.validation_result:
            return ""
        if not self.validation_result.get("ok"):
            return ""
        return self.validation_result.get("normalized_sql", "")

    @property
    def execution_rows(self) -> list[dict[str, Any]] | None:
        if not self.execution_result:
            return None
        if not self.execution_result.get("ok"):
            return None
        return self.execution_result.get("rows", [])

    def to_tool_context(self) -> ToolContext:
        return ToolContext(
            question=self.question,
            tenant_id=self.config.tenant_id,
            table_names=self.table_names or None,
            allowed_tables=self.allowed_tables or None,
            metrics_result=self.metrics_result,
            validated_sql=self.validated_sql,
            execution_rows=self.execution_rows,
            execute_enabled=self.config.execute_enabled,
            dsn=self.config.dsn,
            timeout_ms=self.config.timeout_ms,
            max_limit=self.config.max_limit,
            llm=self.config.llm,
        )
    
    

    def apply_observation(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        
        if tool_name not in _SUPPORTED_TOOLS:
            raise ValueError(f"Unsupported tool observation: {tool_name}")

        if not isinstance(arguments, dict):
            raise TypeError("arguments must be a dict")

        if not isinstance(result, dict):
            raise TypeError("result must be a dict")

        if not isinstance(result.get("ok"), bool):
            raise ValueError("tool result must contain boolean field: ok")
        
        stored_arguments = deepcopy(arguments)
        stored_result = deepcopy(result)

        self.step_count += 1
        if tool_name in self.tool_call_counts:
            self.tool_call_counts[tool_name] += 1
        else:
            self.tool_call_counts[tool_name] = 1

        self.trace.append(
            ToolTraceItem(
                step=self.step_count,
                tool_name=tool_name,
                arguments=stored_arguments,
                observation=deepcopy(stored_result),
                ok=stored_result["ok"],
                message=str(stored_result.get("message", "")),
            )
        )

        succeeded = stored_result["ok"]

        if tool_name == "search_metrics":
            self.metrics_result = stored_result
            self.table_names = _extract_metric_table_names(result) if succeeded else []
            # 指标上下文变化后，基于旧指标得到的后续状态不再可信。
            self.schema_result = None
            self.allowed_tables = []
            self.raw_sql = ""
            self.validation_result = None
            self.execution_result = None
            self.explanation_result = None

        elif tool_name == "search_schema":
            self.schema_result = stored_result

            self.allowed_tables = (
                _extract_allowed_tables(stored_result)
                if succeeded
                else []
            )

            self.validation_result = None
            self.execution_result = None
            self.explanation_result = None

        elif tool_name == "validate_sql":
            sql = arguments.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                raise ValueError("validate_sql observation requires arguments['sql']")

            self.raw_sql = sql
            self.validation_result = stored_result

            # 无论本次校验成功或失败，旧执行结果都不能继续使用。
            self.execution_result = None
            self.explanation_result = None

        elif tool_name == "execute_sql":
            self.execution_result = stored_result

            # 一旦重新执行，旧解释对应的是旧 rows，必须失效。
            self.explanation_result = None

        elif tool_name == "explain_result":
            self.explanation_result = stored_result

        return