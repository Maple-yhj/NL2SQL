"""Typed run state and artifact contracts for execution backends."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field, SerializeAsAny, StringConstraints, model_validator
from pydantic import BaseModel

from data_agent.runtime.composition import ResolvedRuntimeBundle, stable_digest
from data_agent.runtime.models import AgentMode, PrincipalContext

from .models import (
    ArtifactKind,
    BudgetLimits,
    GraphModel,
    GraphSpec,
    PLATFORM_BUDGET_CEILING,
)


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class VersionPin(GraphModel):
    component: NonBlankText
    version: NonBlankText


class ResolvedContext(GraphModel):
    contextualized_question: NonBlankText
    approved_memories: tuple[NonBlankText, ...] = ()
    conversation_summary: NonBlankText | None = None


class ExecutionContext(GraphModel):
    run_id: NonBlankText
    mode: AgentMode
    question: NonBlankText
    enterprise_id: NonBlankText = "olist"
    domain_id: NonBlankText = "commerce"
    principal: PrincipalContext
    bundle: ResolvedRuntimeBundle
    skill_id: NonBlankText
    skill_version: NonBlankText
    allowed_tools: tuple[NonBlankText, ...] = Field(min_length=1)
    tool_versions: tuple[VersionPin, ...] = Field(min_length=1)
    model_versions: tuple[VersionPin, ...] = Field(min_length=1)
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    preview_rows: int = Field(default=20, ge=1, le=100)
    max_estimated_cost: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_pins_and_budget(self) -> "ExecutionContext":
        bundle_skill = self.bundle.skill_versions.get(self.skill_id)
        if bundle_skill != self.skill_version:
            raise ValueError("execution skill version is not pinned by the bundle")
        tool_names = tuple(item.component for item in self.tool_versions)
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("tool version pins must be unique")
        if set(tool_names) != set(self.allowed_tools):
            raise ValueError("every allowed tool must have exactly one version pin")
        model_names = tuple(item.component for item in self.model_versions)
        if len(model_names) != len(set(model_names)):
            raise ValueError("model version pins must be unique")
        for field_name in type(PLATFORM_BUDGET_CEILING).model_fields:
            if getattr(self.budget, field_name) > getattr(
                PLATFORM_BUDGET_CEILING,
                field_name,
            ):
                raise ValueError(f"{field_name} exceeds the platform ceiling")
        return self

    def tool_version(self, tool_name: str) -> str:
        for item in self.tool_versions:
            if item.component == tool_name:
                return item.version
        raise KeyError(tool_name)


class Artifact(GraphModel):
    artifact_id: NonBlankText
    kind: ArtifactKind
    producing_node: NonBlankText
    payload: SerializeAsAny[BaseModel]
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="before")
    @classmethod
    def restore_typed_payload(cls, value: Any) -> Any:
        if isinstance(value, cls) or not isinstance(value, Mapping):
            return value
        raw_kind = value.get("kind")
        raw_payload = value.get("payload")
        if raw_kind is None or raw_payload is None:
            return value
        kind = ArtifactKind(raw_kind)
        payload_type = _artifact_payload_type(kind)
        if isinstance(raw_payload, payload_type):
            return value
        restored = dict(value)
        restored["payload"] = payload_type.model_validate(raw_payload)
        return restored

    @classmethod
    def create(
        cls,
        *,
        kind: ArtifactKind,
        producing_node: str,
        payload: BaseModel,
    ) -> "Artifact":
        digest = stable_digest(
            {
                "kind": kind.value,
                "producing_node": producing_node,
                "payload": payload,
            }
        )
        return cls(
            artifact_id=f"{kind.value}:{digest[:16]}",
            kind=kind,
            producing_node=producing_node,
            payload=payload,
            digest=digest,
        )

    @model_validator(mode="after")
    def validate_digest(self) -> "Artifact":
        payload_type = _artifact_payload_type(self.kind)
        if not isinstance(self.payload, payload_type):
            raise ValueError(
                f"{self.kind.value} artifact requires {payload_type.__name__}"
            )
        expected = self.expected_digest()
        if self.digest != expected:
            raise ValueError("artifact digest does not match its typed payload")
        return self

    def expected_digest(self) -> str:
        return stable_digest(
            {
                "kind": self.kind.value,
                "producing_node": self.producing_node,
                "payload": self.payload,
            }
        )


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ExecutionToolTrace(GraphModel):
    call_id: NonBlankText
    tool_name: NonBlankText
    tool_version: NonBlankText
    status: str
    attempts: int = Field(ge=0)
    error_code: str | None = None


class ExecutionError(GraphModel):
    code: NonBlankText
    message: NonBlankText
    node_id: NonBlankText | None = None
    retryable: bool = False


class RouteAttempt(GraphModel):
    node_id: NonBlankText
    error_code: NonBlankText
    attempts: int = Field(ge=1)


class ExecutionState(GraphModel):
    run_id: NonBlankText
    mode: AgentMode
    status: ExecutionStatus = ExecutionStatus.PENDING
    current_node: NonBlankText | None = None
    next_node: NonBlankText | None = None
    artifacts: tuple[Artifact, ...] = ()
    node_trace: tuple[NonBlankText, ...] = ()
    tool_trace: tuple[ExecutionToolTrace, ...] = ()
    tool_calls: int = Field(default=0, ge=0)
    correction_rounds: int = Field(default=0, ge=0)
    sql_compile_attempts: int = Field(default=0, ge=0)
    result_rows: int = Field(default=0, ge=0)
    route_attempts: tuple[RouteAttempt, ...] = ()
    error: ExecutionError | None = None

    def artifact(self, kind: ArtifactKind) -> Artifact | None:
        return next(
            (artifact for artifact in reversed(self.artifacts) if artifact.kind == kind),
            None,
        )

    def require_artifact(self, kind: ArtifactKind) -> Artifact:
        artifact = self.artifact(kind)
        if artifact is None:
            raise RuntimeError(f"required artifact is missing: {kind.value}")
        return artifact


class EvidenceValidation(GraphModel):
    stage: str
    valid: bool
    logical_plan_hash: NonBlankText
    query_hash: NonBlankText
    policy_decision_id: NonBlankText
    row_count: int = Field(ge=0)


class StaticQueryValidation(GraphModel):
    valid: bool
    logical_plan_hash: NonBlankText
    query_hash: NonBlankText
    policy_decision_id: NonBlankText
    bundle_digest: NonBlankText
    schema_fingerprint: NonBlankText


class FinalOutput(GraphModel):
    status: ExecutionStatus
    mode: AgentMode
    logical_plan_hash: str | None = None
    query_hash: str | None = None
    policy_decision_id: str | None = None
    row_count: int = Field(default=0, ge=0)
    answer: str | None = None
    error_code: str | None = None
    artifact_digests: tuple[str, ...] = ()


def _artifact_payload_type(kind: ArtifactKind) -> type[BaseModel]:
    """Resolve the one public payload model accepted for an artifact kind."""

    from data_agent.runtime.binding import BoundQueryPlan, PreparedQuery
    from data_agent.skills.models import LogicalQueryPlan
    from data_agent.skills.validation import PlanValidationResult
    from data_agent.tools.providers import (
        AnswerRenderOutput,
        QueryData,
        ResultProfileOutput,
        SemanticSearchOutput,
    )
    from data_agent.tools.schemas import CatalogSnapshot, ExplainResult

    return {
        ArtifactKind.RESOLVED_CONTEXT: ResolvedContext,
        ArtifactKind.SEMANTIC_MATCHES: SemanticSearchOutput,
        ArtifactKind.LOGICAL_PLAN: LogicalQueryPlan,
        ArtifactKind.PLAN_VALIDATION: PlanValidationResult,
        ArtifactKind.CATALOG_SNAPSHOT: CatalogSnapshot,
        ArtifactKind.BOUND_QUERY_PLAN: BoundQueryPlan,
        ArtifactKind.PREPARED_QUERY: PreparedQuery,
        ArtifactKind.STATIC_VALIDATION: StaticQueryValidation,
        ArtifactKind.EXPLAIN_RESULT: ExplainResult,
        ArtifactKind.QUERY_PREVIEW: QueryData,
        ArtifactKind.PREVIEW_VALIDATION: EvidenceValidation,
        ArtifactKind.QUERY_RESULT: QueryData,
        ArtifactKind.RESULT_PROFILE: ResultProfileOutput,
        ArtifactKind.RESULT_VALIDATION: EvidenceValidation,
        ArtifactKind.ANSWER: AnswerRenderOutput,
        ArtifactKind.FINAL: FinalOutput,
    }[kind]


class ExecutionResult(GraphModel):
    state: ExecutionState
    final_artifact: Artifact


class ArtifactDigestPin(GraphModel):
    artifact_id: NonBlankText
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ExecutionAuthorityPin(GraphModel):
    tenant_id: NonBlankText
    user_id: NonBlankText
    normalized_roles: tuple[NonBlankText, ...]
    admin_scope: bool
    enterprise_id: NonBlankText
    domain_id: NonBlankText
    request_id: NonBlankText
    question_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @classmethod
    def for_run(cls, context: ExecutionContext) -> "ExecutionAuthorityPin":
        normalized_roles = tuple(
            sorted({role.strip() for role in context.principal.roles})
        )
        tenant_scope = context.bundle.compiled_access_policy.get("tenantScope", {})
        admin_bypass = tenant_scope.get("adminBypass", {})
        allowed_admin_roles = {
            str(role).strip()
            for role in admin_bypass.get("allowedRoles", ())
        }
        return cls(
            tenant_id=context.principal.tenant_id,
            user_id=context.principal.user_id,
            normalized_roles=normalized_roles,
            admin_scope=bool(set(normalized_roles) & allowed_admin_roles),
            enterprise_id=context.enterprise_id,
            domain_id=context.domain_id,
            request_id=context.run_id,
            question_digest=stable_digest({"question": context.question}),
        )


class ExecutionVersionPins(GraphModel):
    authority: ExecutionAuthorityPin
    bundle_digest: NonBlankText
    bundle_runtime_version: NonBlankText
    schema_fingerprint: NonBlankText
    skill_id: NonBlankText
    skill_version: NonBlankText
    graph_id: NonBlankText
    graph_version: NonBlankText
    graph_digest: NonBlankText
    tool_registry_version: NonBlankText
    tool_versions: tuple[VersionPin, ...] = Field(min_length=1)
    model_versions: tuple[VersionPin, ...] = Field(min_length=1)

    @classmethod
    def for_run(
        cls,
        context: ExecutionContext,
        graph: GraphSpec,
    ) -> "ExecutionVersionPins":
        return cls(
            authority=ExecutionAuthorityPin.for_run(context),
            bundle_digest=context.bundle.digest,
            bundle_runtime_version=context.bundle.runtime_version,
            schema_fingerprint=context.bundle.schema_fingerprint,
            skill_id=context.skill_id,
            skill_version=context.skill_version,
            graph_id=graph.graph_id,
            graph_version=graph.version,
            graph_digest=graph.digest,
            tool_registry_version=context.bundle.tool_registry_version,
            tool_versions=context.tool_versions,
            model_versions=context.model_versions,
        )


class ExecutionCheckpoint(GraphModel):
    checkpoint_id: NonBlankText
    pins: ExecutionVersionPins
    state: ExecutionState
    artifact_digests: tuple[ArtifactDigestPin, ...]
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @classmethod
    def capture(
        cls,
        *,
        pins: ExecutionVersionPins,
        state: ExecutionState,
    ) -> "ExecutionCheckpoint":
        artifact_digests = tuple(
            ArtifactDigestPin(
                artifact_id=artifact.artifact_id,
                digest=artifact.digest,
            )
            for artifact in state.artifacts
        )
        payload = {
            "pins": pins,
            "state": state,
            "artifact_digests": artifact_digests,
        }
        digest = stable_digest(payload)
        return cls(
            checkpoint_id=f"checkpoint:{digest[:16]}",
            pins=pins,
            state=state,
            artifact_digests=artifact_digests,
            digest=digest,
        )

    def integrity_error(self) -> str | None:
        if self.state.status != ExecutionStatus.PAUSED or self.state.next_node is None:
            return "checkpoint state must be paused before a next node"
        observed = tuple(
            ArtifactDigestPin(
                artifact_id=artifact.artifact_id,
                digest=artifact.digest,
            )
            for artifact in self.state.artifacts
        )
        if self.artifact_digests != observed:
            return "checkpoint artifact digests do not match state"
        if any(
            artifact.digest != artifact.expected_digest()
            for artifact in self.state.artifacts
        ):
            return "checkpoint contains an artifact with an invalid digest"
        expected = stable_digest(
            {
                "pins": self.pins,
                "state": self.state,
                "artifact_digests": self.artifact_digests,
            }
        )
        if self.digest != expected:
            return "checkpoint digest does not match its payload"
        if self.checkpoint_id != f"checkpoint:{expected[:16]}":
            return "checkpoint id does not match its digest"
        return None

    @model_validator(mode="after")
    def validate_integrity(self) -> "ExecutionCheckpoint":
        error = self.integrity_error()
        if error is not None:
            raise ValueError(error)
        return self
