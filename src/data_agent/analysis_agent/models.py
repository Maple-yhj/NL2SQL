"""Strict, checkpoint-safe contracts for the native data analysis agent."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StringConstraints, field_validator, model_validator
from pydantic_core import to_jsonable_python

from data_agent.public_contracts import (
    AgentError,
    AgentMode,
    Digest,
    NonBlankText,
    PublicContractModel,
    SchemaFingerprint,
)


Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
ToolName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$",
    ),
]


def stable_digest(value: object) -> str:
    """Return a stable SHA-256 digest of canonical JSON-compatible data."""

    jsonable = to_jsonable_python(value)
    encoded = json.dumps(
        jsonable,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return values


_FORBIDDEN_MODEL_ARGUMENT_KEYS = frozenset(
    {
        "authority",
        "authority_envelope",
        "tenant",
        "tenant_id",
        "user",
        "user_id",
        "principal",
        "principal_id",
        "source",
        "source_id",
        "source_version",
        "binding",
        "binding_id",
        "binding_version",
        "schema_fingerprint",
        "allowed_relation_ids",
        "credential",
        "credentials",
        "credential_ref",
        "secret",
        "password",
        "api_key",
        "token",
        "dsn",
        "connection_string",
        "raw_sql",
        "sql",
        "query_text",
        "code",
        "python",
        "javascript",
        "shell",
        "command",
        "file_path",
        "filepath",
        "path",
    }
)


def _find_forbidden_argument_key(value: JsonValue, *, path: str = "arguments") -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = key.strip().lower().replace("-", "_")
            nested_path = f"{path}.{key}"
            if normalized in _FORBIDDEN_MODEL_ARGUMENT_KEYS:
                return nested_path
            result = _find_forbidden_argument_key(nested, path=nested_path)
            if result is not None:
                return result
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            result = _find_forbidden_argument_key(nested, path=f"{path}[{index}]")
            if result is not None:
                return result
    return None


class AgentStatus(StrEnum):
    INITIALIZING = "initializing"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentInputReason(StrEnum):
    CLARIFICATION = "clarification"
    APPROVAL = "approval"
    CONFLICT_RESOLUTION = "conflict_resolution"


class AgentArtifactKind(StrEnum):
    CATALOG = "catalog"
    LOGICAL_PLAN = "logical_plan"
    PREPARED_QUERY = "prepared_query"
    QUERY_PREVIEW = "query_preview"
    QUERY_RESULT = "query_result"
    PROFILE = "profile"
    COMPUTATION = "computation"
    CHART = "chart"
    ANSWER = "answer"


class DatasetAuthority(PublicContractModel):
    kind: Literal["dataset"] = "dataset"
    tenant_id: NonBlankText
    user_id: NonBlankText
    source_id: NonBlankText
    source_version: int = Field(ge=1)
    binding_id: NonBlankText
    binding_version: int = Field(ge=1)
    schema_fingerprint: SchemaFingerprint
    allowed_relation_ids: tuple[NonBlankText, ...] = Field(min_length=1)
    mode: AgentMode

    @field_validator("allowed_relation_ids")
    @classmethod
    def validate_relation_allowlist(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _validate_unique(values, "allowed relation ids")


class AnalysisGoal(PublicContractModel):
    original_question: NonBlankText
    contextualized_question: NonBlankText
    requested_output: NonBlankText
    success_criteria: tuple[NonBlankText, ...] = Field(min_length=1)
    constraints: tuple[NonBlankText, ...] = ()

    @field_validator("success_criteria", "constraints")
    @classmethod
    def validate_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_unique(values, "goal entries")


class AnalysisStep(PublicContractModel):
    step_id: Identifier
    objective: NonBlankText
    status: Literal["pending", "running", "completed", "blocked", "skipped"]
    depends_on: tuple[Identifier, ...] = ()
    expected_evidence: tuple[NonBlankText, ...] = ()

    @field_validator("depends_on")
    @classmethod
    def validate_unique_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_unique(values, "step dependencies")

    @field_validator("expected_evidence")
    @classmethod
    def validate_unique_expected_evidence(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _validate_unique(values, "expected evidence entries")

    @model_validator(mode="after")
    def validate_no_self_dependency(self) -> Self:
        if self.step_id in self.depends_on:
            raise ValueError("analysis step cannot depend on itself")
        return self


class AnalysisPlan(PublicContractModel):
    plan_id: Identifier
    revision: int = Field(ge=1)
    steps: tuple[AnalysisStep, ...] = Field(min_length=1)
    completion_criteria: tuple[NonBlankText, ...] = Field(min_length=1)

    @field_validator("completion_criteria")
    @classmethod
    def validate_unique_criteria(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_unique(values, "completion criteria")

    @model_validator(mode="after")
    def validate_step_graph(self) -> Self:
        step_ids = tuple(step.step_id for step in self.steps)
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("analysis plan step ids must be unique")
        known = set(step_ids)
        dependencies = {step.step_id: set(step.depends_on) for step in self.steps}
        unknown = sorted(
            dependency
            for values in dependencies.values()
            for dependency in values
            if dependency not in known
        )
        if unknown:
            raise ValueError(f"analysis plan contains unknown dependencies: {unknown}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("analysis plan dependency cycle detected")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_ids:
            visit(step_id)
        return self


class AgentAction(PublicContractModel):
    action_id: Identifier
    tool_name: ToolName
    arguments: dict[NonBlankText, JsonValue]
    purpose: NonBlankText
    expected_evidence: tuple[NonBlankText, ...]

    @field_validator("arguments")
    @classmethod
    def reject_authority_and_escape_hatches(
        cls,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        forbidden = _find_forbidden_argument_key(arguments)
        if forbidden is not None:
            raise ValueError(f"forbidden model-controlled argument: {forbidden}")
        return arguments

    @field_validator("expected_evidence")
    @classmethod
    def validate_unique_evidence_expectations(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _validate_unique(values, "expected evidence entries")


class AgentArtifactRef(PublicContractModel):
    artifact_id: Identifier
    run_id: Identifier
    kind: AgentArtifactKind
    digest: Digest
    schema_digest: SchemaFingerprint | None = None
    row_count: int | None = Field(default=None, ge=0)
    sensitivity: Literal["metadata", "derived", "row_data"]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("artifact created_at must be timezone-aware")
        return value


class EvidenceRef(PublicContractModel):
    evidence_id: Identifier
    claim_key: Identifier
    artifact_id: Identifier
    source_id: NonBlankText
    source_version: int = Field(ge=1)
    binding_id: NonBlankText
    binding_version: int = Field(ge=1)
    schema_fingerprint: SchemaFingerprint
    sql_digest: Digest | None = None
    result_digest: Digest
    field_refs: tuple[NonBlankText, ...] = Field(min_length=1)

    @field_validator("field_refs")
    @classmethod
    def validate_unique_field_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_unique(values, "evidence field refs")


class AgentObservation(PublicContractModel):
    observation_id: Identifier
    action_id: Identifier
    tool_name: ToolName
    status: Literal["succeeded", "failed"]
    summary: NonBlankText
    artifact_refs: tuple[AgentArtifactRef, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    safe_preview: tuple[dict[NonBlankText, JsonValue], ...] = ()
    error: AgentError | None = None

    @model_validator(mode="after")
    def validate_result_integrity(self) -> Self:
        if self.status == "succeeded" and self.error is not None:
            raise ValueError("successful observation must not contain an error")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed observation requires an error")

        artifact_ids = tuple(item.artifact_id for item in self.artifact_refs)
        evidence_ids = tuple(item.evidence_id for item in self.evidence_refs)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("observation artifact ids must be unique")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("observation evidence ids must be unique")
        available = set(artifact_ids)
        for item in self.evidence_refs:
            if item.artifact_id not in available:
                raise ValueError("evidence must reference an observation artifact")
            artifact = next(
                candidate
                for candidate in self.artifact_refs
                if candidate.artifact_id == item.artifact_id
            )
            if item.result_digest != artifact.digest:
                raise ValueError("evidence result digest must match its artifact")
        return self


class AgentInputRequest(PublicContractModel):
    interrupt_id: Identifier
    reason: AgentInputReason
    prompt: NonBlankText
    choices: tuple[NonBlankText, ...] = ()
    allow_free_text: bool = True
    action_id: Identifier | None = None

    @field_validator("choices")
    @classmethod
    def validate_unique_choices(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_unique(values, "input choices")

    @model_validator(mode="after")
    def validate_response_options(self) -> Self:
        if not self.allow_free_text and not self.choices:
            raise ValueError("input request must accept free text or provide choices")
        if self.reason == AgentInputReason.APPROVAL and self.action_id is None:
            raise ValueError("approval input requires an action_id")
        return self


class PlannerDecision(PublicContractModel):
    plan: AnalysisPlan
    decision: Literal["act", "clarify", "finish", "fail"]
    next_action: AgentAction | None = None
    clarification: AgentInputRequest | None = None
    completion_summary: NonBlankText | None = None
    rationale_summary: NonBlankText

    @model_validator(mode="after")
    def validate_decision_fields(self) -> Self:
        if self.decision == "act":
            if self.next_action is None:
                raise ValueError("act decision requires next_action")
            if self.clarification is not None or self.completion_summary is not None:
                raise ValueError("act decision cannot include clarification or completion")
        elif self.decision == "clarify":
            if self.clarification is None:
                raise ValueError("clarify decision requires clarification")
            if self.next_action is not None or self.completion_summary is not None:
                raise ValueError("clarify decision cannot include next_action or completion")
        elif self.decision == "finish":
            if self.completion_summary is None:
                raise ValueError("finish decision requires completion_summary")
            if self.next_action is not None or self.clarification is not None:
                raise ValueError("finish decision cannot include next_action or clarification")
        else:
            if any(
                value is not None
                for value in (self.next_action, self.clarification, self.completion_summary)
            ):
                raise ValueError("fail decision cannot include action, clarification, or completion")
        return self


class EvaluationDecision(PublicContractModel):
    decision: Literal["continue", "replan", "clarify", "finish", "fail"]
    evidence_sufficient: bool
    completed_step_ids: tuple[Identifier, ...]
    missing_evidence: tuple[NonBlankText, ...]
    contradictions: tuple[NonBlankText, ...]
    clarification: AgentInputRequest | None = None
    rationale_summary: NonBlankText

    @field_validator("completed_step_ids", "missing_evidence", "contradictions")
    @classmethod
    def validate_unique_evaluation_entries(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _validate_unique(values, "evaluation entries")

    @model_validator(mode="after")
    def validate_evaluation_fields(self) -> Self:
        if self.decision == "clarify":
            if self.clarification is None:
                raise ValueError("clarify evaluation requires clarification")
        elif self.clarification is not None:
            raise ValueError("only clarify evaluation may include clarification")
        if self.decision == "finish" and not self.evidence_sufficient:
            raise ValueError("finish evaluation requires sufficient evidence")
        if self.decision != "finish" and self.evidence_sufficient:
            raise ValueError("only finish evaluation may claim sufficient evidence")
        return self


class FindingDraft(PublicContractModel):
    finding_id: Identifier
    claim: NonBlankText
    evidence_ids: tuple[Identifier, ...]

    @field_validator("evidence_ids")
    @classmethod
    def validate_unique_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_unique(values, "finding evidence ids")


_NUMERIC_CLAIM = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)*(?:%|\b)")


class AgentAnswerDraft(PublicContractModel):
    answer: NonBlankText
    key_findings: tuple[FindingDraft, ...]
    recommended_chart_artifact_id: Identifier | None = None
    limitations: tuple[NonBlankText, ...]
    evidence_ids: tuple[Identifier, ...]

    @field_validator("limitations", "evidence_ids")
    @classmethod
    def validate_unique_answer_entries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_unique(values, "answer entries")

    @model_validator(mode="after")
    def validate_grounding(self) -> Self:
        finding_ids = tuple(item.finding_id for item in self.key_findings)
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding ids must be unique")
        allowed_evidence = set(self.evidence_ids)
        for finding in self.key_findings:
            if not set(finding.evidence_ids).issubset(allowed_evidence):
                raise ValueError("finding references evidence outside the answer")
            if _NUMERIC_CLAIM.search(finding.claim) and not finding.evidence_ids:
                raise ValueError("numeric finding requires evidence")
        return self


class AgentRunBudget(PublicContractModel):
    max_agent_steps: int = Field(default=16, ge=1)
    max_model_calls: int = Field(default=10, ge=1)
    max_tool_calls: int = Field(default=24, ge=1)
    max_query_compiles: int = Field(default=8, ge=1)
    max_query_previews: int = Field(default=6, ge=0)
    max_query_executes: int = Field(default=4, ge=0)
    max_replans: int = Field(default=4, ge=0)
    max_result_rows: int = Field(default=1000, ge=1)
    max_observation_cells_for_model: int = Field(default=400, ge=1)
    max_duration_seconds: int = Field(default=180, ge=1)


class AgentBudgetState(PublicContractModel):
    agent_steps: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    query_compiles: int = Field(default=0, ge=0)
    query_previews: int = Field(default=0, ge=0)
    query_executes: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)
    started_at: datetime
    deadline_at: datetime

    @model_validator(mode="after")
    def validate_time_window(self) -> Self:
        if self.started_at.tzinfo is None or self.deadline_at.tzinfo is None:
            raise ValueError("budget timestamps must be timezone-aware")
        if self.deadline_at <= self.started_at:
            raise ValueError("budget deadline must be after start")
        return self


class AgentContextSnapshot(PublicContractModel):
    catalog_digest: Digest
    binding_digest: Digest
    relationship_graph_digest: Digest | None = None
    catalog_summary: dict[NonBlankText, JsonValue] = Field(default_factory=dict)
    semantic_summary: dict[NonBlankText, JsonValue] = Field(default_factory=dict)
    conversation_summary: NonBlankText | None = None
    allowed_tool_names: tuple[ToolName, ...] = Field(min_length=1)

    @field_validator("allowed_tool_names")
    @classmethod
    def validate_unique_tool_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_unique(values, "allowed tool names")


__all__ = [
    "AgentAction",
    "AgentAnswerDraft",
    "AgentArtifactKind",
    "AgentArtifactRef",
    "AgentBudgetState",
    "AgentContextSnapshot",
    "AgentError",
    "AgentInputReason",
    "AgentInputRequest",
    "AgentObservation",
    "AgentRunBudget",
    "AgentStatus",
    "AnalysisGoal",
    "AnalysisPlan",
    "AnalysisStep",
    "DatasetAuthority",
    "EvaluationDecision",
    "EvidenceRef",
    "FindingDraft",
    "Identifier",
    "PlannerDecision",
    "stable_digest",
]
