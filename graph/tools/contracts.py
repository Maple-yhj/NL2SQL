from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ToolHandler = Callable[[dict[str, Any], Any, dict[str, Any]], Awaitable[dict[str, Any]]]
RiskLevel = Literal["low", "medium", "high"]
SideEffects = Literal["none", "read", "write"]
ResponseFormat = Literal["concise", "detailed", "debug"]


async def unbound_tool_handler(
    state: dict[str, Any],
    runtime: Any,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    raise NotImplementedError("ToolSpec is metadata-only and has no bound handler.")


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=0, ge=0)
    retry_on_codes: tuple[str, ...] = ()


class ToolExample(BaseModel):
    description: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)


class ToolError(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False
    retry_hint: str | None = None
    suggested_inputs: dict[str, Any] | None = None


class ToolWarning(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ArtifactRef(BaseModel):
    artifact_type: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    description: str = ""


class ToolResult(BaseModel):
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    warnings: list[ToolWarning] = Field(default_factory=list)
    error: ToolError | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_error_for_failure(self) -> ToolResult:
        if not self.ok and self.error is None:
            raise ValueError("failed ToolResult requires error")
        return self


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    aliases: tuple[str, ...] = ()
    description: str
    input_keys: tuple[str, ...] = ()
    output_keys: tuple[str, ...] = ()
    input_schema: type[BaseModel] | None = None
    output_schema: type[BaseModel] | None = None
    requires_llm: bool = False
    requires_embeddings: bool = False
    requires_db: bool = False
    risk_level: RiskLevel = "low"
    side_effects: SideEffects = "none"
    handler: ToolHandler = Field(default=unbound_tool_handler, exclude=True, repr=False)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    response_formats: tuple[ResponseFormat, ...] = ("concise",)
    examples: tuple[ToolExample, ...] = ()
    eval_tags: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("ToolSpec.name must not be blank")
        return stripped

    @field_validator("aliases")
    @classmethod
    def strip_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        aliases = tuple(alias.strip() for alias in value if alias.strip())
        if len(set(aliases)) != len(aliases):
            raise ValueError("ToolSpec.aliases must not contain duplicates")
        return aliases


class PrepareSqlInput(BaseModel):
    sql: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    allowed_tables: list[str] = Field(default_factory=list)
    max_limit: int = Field(default=1000, gt=0)
    domain_constraints: dict[str, Any] | None = None


class PrepareSqlOutput(BaseModel):
    valid: bool | None = None
    ok: bool
    sql: str
    normalized_sql: str = ""
    executable_sql: str = ""
    violations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[Any] = Field(default_factory=list)


class ValidateSqlInput(BaseModel):
    sql: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    allowed_tables: list[str] = Field(default_factory=list)
    max_limit: int = Field(default=1000, gt=0)


class ValidateSqlOutput(BaseModel):
    ok: bool
    sql: str
    normalized_sql: str = ""
    tenant_id: str
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    limit: int | None = None
    violations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[Any] = Field(default_factory=list)
    message: str = ""


class ExecuteSqlInput(BaseModel):
    sql: str | None = None
    validated_sql: str | None = None
    tenant_id: str = Field(min_length=1)


class ExecuteSqlOutput(BaseModel):
    ok: bool
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    error: str = ""


class ContextualizeQuestionInput(BaseModel):
    question: str = Field(min_length=1)
    conversation_history: list[dict[str, Any]] = Field(default_factory=list)
    user_memories: list[dict[str, Any]] = Field(default_factory=list)


class ContextualizeQuestionOutput(BaseModel):
    contextualized_question: str


class SearchMetricsInput(BaseModel):
    question: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)


class SearchMetricsOutput(BaseModel):
    metrics_result: dict[str, Any] = Field(default_factory=dict)
    table_names: list[str] = Field(default_factory=list)
    domain_context: str = ""
    domain_constraints: dict[str, Any] = Field(default_factory=dict)


class ResolveDomainRulesInput(BaseModel):
    question: str = Field(min_length=1)
    intent: dict[str, Any] = Field(default_factory=dict)
    metrics_result: dict[str, Any] = Field(default_factory=dict)


class ResolveDomainRulesOutput(BaseModel):
    domain_context: str = ""
    domain_constraints: dict[str, Any] = Field(default_factory=dict)
    table_names: list[str] = Field(default_factory=list)


class SearchSchemaInput(BaseModel):
    question: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    table_names: list[str] = Field(default_factory=list)


class SearchSchemaOutput(BaseModel):
    schema_result: dict[str, Any] = Field(default_factory=dict)
    allowed_tables: list[str] = Field(default_factory=list)


class GenerateSqlInput(BaseModel):
    question: str = Field(min_length=1)
    intent: dict[str, Any] = Field(default_factory=dict)
    metrics_result: dict[str, Any] = Field(default_factory=dict)
    schema_result: dict[str, Any] = Field(default_factory=dict)


class GenerateSqlOutput(BaseModel):
    candidate_sql: str


class ExplainResultInput(BaseModel):
    question: str = Field(min_length=1)
    validated_sql: str = Field(min_length=1)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    metrics_result: dict[str, Any] = Field(default_factory=dict)


class ExplainResultOutput(BaseModel):
    answer: str
