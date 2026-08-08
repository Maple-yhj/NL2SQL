"""Typed, immutable contracts for governed Data Agent tools."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializeAsAny,
    StringConstraints,
    field_validator,
    model_validator,
)

from data_agent.runtime.models import AgentMode, PrincipalContext
from data_agent.analysis_agent.models import DatasetAuthority


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ToolName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z][a-z0-9]*\.[a-z][a-z0-9]*$",
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


class ToolModel(BaseModel):
    """Frozen exact-field base for all public Tool contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    def model_copy(
        self,
        *,
        update: dict[str, object] | None = None,
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


class RetryPolicy(ToolModel):
    max_attempts: int = Field(default=1, ge=1, le=5)
    initial_backoff_seconds: float = Field(default=0.05, ge=0, le=5)
    max_backoff_seconds: float = Field(default=1.0, ge=0, le=10)


class ToolExample(ToolModel):
    input_data: SerializeAsAny[BaseModel]
    output_data: SerializeAsAny[BaseModel]


class ToolSpec(ToolModel):
    name: ToolName
    version: SemanticVersion
    description: NonBlankText
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    risk_level: Literal["low", "medium", "high"]
    side_effects: Literal["none", "read"]
    required_capabilities: tuple[ToolName, ...] = Field(min_length=1)
    idempotency: Literal["none", "safe", "required"]
    timeout_seconds: float = Field(gt=0, le=120)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    examples: tuple[ToolExample, ...] = ()
    eval_tags: tuple[NonBlankText, ...] = ()
    authority_kinds: tuple[Literal["dataset"], ...] = ("dataset",)
    allowed_modes: tuple[AgentMode, ...] = (
        AgentMode.PLAN,
        AgentMode.PREVIEW,
        AgentMode.EXECUTE,
    )
    artifact_policy: Literal["none", "metadata", "derived", "row_data"] = "none"
    credential_requirement: Literal["none", "required"] | None = None

    @model_validator(mode="after")
    def normalize_authority_policy(self) -> "ToolSpec":
        if len(self.authority_kinds) != len(set(self.authority_kinds)):
            raise ValueError("tool authority kinds must be unique")
        if len(self.allowed_modes) != len(set(self.allowed_modes)):
            raise ValueError("tool allowed modes must be unique")
        if not self.authority_kinds or not self.allowed_modes:
            raise ValueError("tool must allow at least one authority kind and mode")
        requirement = self.credential_requirement
        if requirement is None:
            requirement = "required" if self.side_effects == "read" else "none"
            object.__setattr__(self, "credential_requirement", requirement)
        if requirement == "required" and self.side_effects != "read":
            raise ValueError("credential tools must declare read side effects")
        return self


class ToolCall(ToolModel):
    call_id: NonBlankText
    tool_name: ToolName
    tool_version: SemanticVersion = "1.0.0"
    input_data: SerializeAsAny[BaseModel]
    idempotency_key: NonBlankText | None = None

    @field_validator("input_data")
    @classmethod
    def require_typed_input(cls, value: BaseModel) -> BaseModel:
        if type(value) is BaseModel:
            raise ValueError("tool input must use a concrete Pydantic model")
        return value


class ToolErrorCode(StrEnum):
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    INPUT_INVALID = "INPUT_INVALID"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    ACCESS_DENIED = "ACCESS_DENIED"
    GRANT_INVALID = "GRANT_INVALID"
    GRANT_EXPIRED = "GRANT_EXPIRED"
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    LOGICAL_PLAN_INVALID = "LOGICAL_PLAN_INVALID"
    BINDING_STALE = "BINDING_STALE"
    SQL_COMPILE_ERROR = "SQL_COMPILE_ERROR"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    RELATION_NOT_ALLOWED = "RELATION_NOT_ALLOWED"
    ROW_LIMIT_EXCEEDED = "ROW_LIMIT_EXCEEDED"
    CONNECTOR_UNAVAILABLE = "CONNECTOR_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ToolError(ToolModel):
    code: ToolErrorCode
    message: NonBlankText
    retryable: bool = False


class ArtifactRef(ToolModel):
    artifact_id: NonBlankText
    media_type: NonBlankText


class ToolLineage(ToolModel):
    logical_refs: tuple[NonBlankText, ...] = ()
    physical_relations: tuple[NonBlankText, ...] = ()
    logical_plan_hash: NonBlankText | None = None
    query_hash: NonBlankText | None = None
    evidence_ids: tuple[NonBlankText, ...] = ()


class ToolTrace(ToolModel):
    call_id: NonBlankText
    tool_name: ToolName
    tool_version: SemanticVersion
    status: Literal["success", "error"]
    attempts: int = Field(ge=0)
    started_at: datetime
    finished_at: datetime
    latency_ms: int = Field(ge=0)
    input_schema: NonBlankText
    output_schema: NonBlankText
    safe_args_digest: NonBlankText | None = None
    artifact_ids: tuple[NonBlankText, ...] = ()
    evidence_ids: tuple[NonBlankText, ...] = ()
    error_code: ToolErrorCode | None = None


class ToolResult(ToolModel):
    status: Literal["success", "error"]
    typed_data: SerializeAsAny[BaseModel] | None = None
    artifact_refs: tuple[ArtifactRef, ...] = ()
    structured_error: ToolError | None = None
    warnings: tuple[NonBlankText, ...] = ()
    rows: int = Field(default=0, ge=0)
    cost: float | None = Field(default=None, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    lineage: ToolLineage = Field(default_factory=ToolLineage)
    policy_decision_id: NonBlankText | None = None
    redacted_trace: ToolTrace

    @model_validator(mode="after")
    def validate_result_state(self) -> "ToolResult":
        if self.status == "success":
            if self.typed_data is None or self.structured_error is not None:
                raise ValueError("successful tool result requires data and no error")
        elif self.structured_error is None or self.typed_data is not None:
            raise ValueError("failed tool result requires an error and no data")
        return self


class AccessGrant(ToolModel):
    grant_id: NonBlankText
    tool_name: ToolName
    tool_version: SemanticVersion
    skill_id: NonBlankText
    bundle_digest: NonBlankText
    schema_fingerprint: NonBlankText
    source: NonBlankText | None = None
    read_only: Literal[True] = True
    principal_user_id: NonBlankText
    tenant_id: NonBlankText
    admin_bypass: bool
    allowed_relations: tuple[NonBlankText, ...]
    max_rows: int = Field(ge=1)
    statement_timeout_ms: int = Field(ge=1)
    policy_decision_id: NonBlankText
    logical_plan_hash: NonBlankText | None = None
    prepared_query_hash: NonBlankText | None = None
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_lifetime(self) -> "AccessGrant":
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("access grant timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("access grant must expire after it is issued")
        if (self.expires_at - self.issued_at).total_seconds() > 300:
            raise ValueError("access grant lifetime must be short")
        return self


class CredentialLease(ToolModel):
    credential_id: NonBlankText
    grant_id: NonBlankText
    bundle_digest: NonBlankText
    source: NonBlankText
    connection_ref: NonBlankText
    capabilities: tuple[ToolName, ...] = Field(min_length=1)
    secret: SecretStr = Field(exclude=True, repr=False)
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_lease_authority(self) -> "CredentialLease":
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("credential lease timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("credential lease must expire after issue")
        if (self.expires_at - self.issued_at).total_seconds() > 300:
            raise ValueError("credential lease lifetime must be short")
        return self


class ToolBudget:
    """Atomic per-run tool call counter shared by concurrent invocations."""

    __slots__ = ("max_calls", "_used_calls", "_lock")

    def __init__(self, *, max_calls: int) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.max_calls = max_calls
        self._used_calls = 0
        self._lock = asyncio.Lock()

    @property
    def used_calls(self) -> int:
        return self._used_calls

    async def consume(self) -> bool:
        async with self._lock:
            if self._used_calls >= self.max_calls:
                return False
            self._used_calls += 1
            return True


AuthorityEnvelope = DatasetAuthority


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    principal: PrincipalContext
    skill_id: str
    skill_version: str
    allowed_tools: tuple[str, ...]
    budget: ToolBudget
    authority: DatasetAuthority
    mode: AgentMode | None = None
    runtime_resources: object | None = None
    max_rows: int = 1000
    statement_timeout_ms: int = 15_000
    run_id: str = "run-tool"

    def __post_init__(self) -> None:
        if self.max_rows < 1 or self.statement_timeout_ms < 1:
            raise ValueError("dataset tool runtime limits must be positive")
        authority = DatasetAuthority.model_validate(self.authority)
        effective_mode = self.mode or authority.mode
        if effective_mode != authority.mode:
            raise ValueError("dataset authority mode is immutable")
        if (
            self.principal.tenant_id != authority.tenant_id
            or self.principal.user_id != authority.user_id
        ):
            raise ValueError("dataset authority does not belong to the principal")
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "mode", effective_mode)


@dataclass(frozen=True, slots=True)
class ProviderContext:
    call_id: str
    run_id: str
    principal: PrincipalContext
    authority: DatasetAuthority
    runtime_resources: object | None
    access_grant: AccessGrant
    credential: CredentialLease | None


@runtime_checkable
class ToolProvider(Protocol):
    spec: ToolSpec

    async def invoke(
        self,
        payload: BaseModel,
        context: ProviderContext,
    ) -> BaseModel: ...


@runtime_checkable
class CredentialBroker(Protocol):
    async def acquire(
        self,
        *,
        grant: AccessGrant,
        source: str | None,
    ) -> CredentialLease | None: ...


class NullCredentialBroker:
    async def acquire(
        self,
        *,
        grant: AccessGrant,
        source: str | None,
    ) -> CredentialLease | None:
        return None
