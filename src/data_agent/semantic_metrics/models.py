"""Versioned semantic metric definitions and governance protocol models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .ast import (
    MetricAstModel,
    MetricFormulaExpression,
    MetricPredicate,
    MetricScalar,
    analyze_metric_ast,
)
from .digest import semantic_digest
from .types import NonBlankText, StableIdentifier


class MetricSetStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    REVOKED = "revoked"


class MetricProposalStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_CLARIFICATION = "needs_clarification"
    VALIDATED = "validated"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class MetricRiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MetricProvenanceKind(StrEnum):
    DOMAIN_PACK = "domain_pack"
    WEB = "web"
    USER = "user"
    SCHEMA = "schema"
    PROFILE = "profile"
    LEGACY_ADAPTER = "legacy_adapter"


class MetricNullPolicy(StrEnum):
    EXCLUDE = "exclude"
    ZERO = "zero"
    ERROR = "error"


class MetricProvenance(MetricAstModel):
    kind: MetricProvenanceKind
    reference: NonBlankText
    digest: NonBlankText | None = None
    retrieved_at: datetime | None = None


class MetricScopeConvention(MetricAstModel):
    status_ref: NonBlankText | None = None
    included_statuses: tuple[NonBlankText, ...] = ()
    excluded_statuses: tuple[NonBlankText, ...] = ()
    refund_treatment: Literal[
        "gross",
        "exclude_refunded",
        "net_of_refunds",
        "not_available",
    ] = "gross"
    refund_ref: NonBlankText | None = None
    includes_freight: bool | None = None
    includes_tax: bool | None = None
    notes: NonBlankText | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "MetricScopeConvention":
        if (self.included_statuses or self.excluded_statuses) and self.status_ref is None:
            raise ValueError("status conventions require status_ref")
        included = {item.casefold() for item in self.included_statuses}
        excluded = {item.casefold() for item in self.excluded_statuses}
        if len(included) != len(self.included_statuses):
            raise ValueError("included statuses must be unique")
        if len(excluded) != len(self.excluded_statuses):
            raise ValueError("excluded statuses must be unique")
        if included.intersection(excluded):
            raise ValueError("a status cannot be both included and excluded")
        if self.refund_treatment in {"exclude_refunded", "net_of_refunds"}:
            if self.refund_ref is None:
                raise ValueError("refund treatment requires refund_ref")
        return self


class SemanticMetricDefinitionV2(MetricAstModel):
    schema_version: Literal[2] = 2
    metric_ref: NonBlankText
    display_name: NonBlankText
    description: NonBlankText
    synonyms: tuple[NonBlankText, ...] = ()
    formula: MetricFormulaExpression
    default_filter: MetricPredicate | None = None
    default_time_ref: NonBlankText | None = None
    allowed_time_refs: tuple[NonBlankText, ...] = ()
    entity_key_refs: tuple[NonBlankText, ...] = ()
    grain: NonBlankText | None = None
    unit: NonBlankText | None = None
    currency: NonBlankText | None = None
    currency_ref: NonBlankText | None = None
    null_policy: MetricNullPolicy = MetricNullPolicy.EXCLUDE
    scope: MetricScopeConvention = Field(default_factory=MetricScopeConvention)
    limitations: tuple[NonBlankText, ...] = ()
    owner: NonBlankText | None = None
    provenance: tuple[MetricProvenance, ...] = ()

    @field_validator(
        "synonyms",
        "allowed_time_refs",
        "entity_key_refs",
        "limitations",
    )
    @classmethod
    def validate_unique_text_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(item.casefold() for item in values)):
            raise ValueError("semantic metric values must be unique")
        return values

    @model_validator(mode="after")
    def validate_definition(self) -> "SemanticMetricDefinitionV2":
        analyze_metric_ast(self.formula, self.default_filter)
        if (
            self.default_time_ref is not None
            and self.allowed_time_refs
            and self.default_time_ref not in self.allowed_time_refs
        ):
            raise ValueError("default_time_ref must be one of allowed_time_refs")
        if self.currency is not None and self.currency_ref is not None:
            raise ValueError("use either a fixed currency or currency_ref, not both")
        return self

    @property
    def ast_field_refs(self) -> tuple[str, ...]:
        return analyze_metric_ast(self.formula, self.default_filter).field_refs

    @property
    def all_field_refs(self) -> tuple[str, ...]:
        refs = [*self.ast_field_refs]
        for value in (
            self.default_time_ref,
            self.currency_ref,
            self.scope.status_ref,
            self.scope.refund_ref,
        ):
            if value is not None:
                refs.append(value)
        refs.extend(self.allowed_time_refs)
        refs.extend(self.entity_key_refs)
        return tuple(dict.fromkeys(refs))


class MetricSetIdentity(MetricAstModel):
    metric_set_id: StableIdentifier
    version: int = Field(ge=1)
    digest: NonBlankText


class MetricContextPin(MetricAstModel):
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    source_version: int = Field(ge=1)
    binding_id: StableIdentifier
    binding_version: int = Field(ge=1)
    metric_set: MetricSetIdentity | None = None
    overlay_id: StableIdentifier | None = None
    overlay_digest: NonBlankText | None = None
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_overlay_pin(self) -> "MetricContextPin":
        if (self.overlay_id is None) != (self.overlay_digest is None):
            raise ValueError("overlay id and digest must be pinned together")
        return self


class DomainPackIdentity(MetricAstModel):
    pack_id: StableIdentifier
    version: NonBlankText
    digest: NonBlankText
    domain_id: StableIdentifier


class MetricSetRecord(MetricAstModel):
    schema_version: Literal[1] = 1
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    source_snapshot_version: int = Field(ge=1)
    schema_fingerprint: NonBlankText
    domain_id: StableIdentifier
    binding_id: StableIdentifier
    binding_version: int = Field(ge=1)
    metric_set_id: StableIdentifier
    version: int = Field(ge=1)
    status: MetricSetStatus = MetricSetStatus.DRAFT
    definitions: tuple[SemanticMetricDefinitionV2, ...] = Field(min_length=1)
    content_digest: NonBlankText
    created_by: NonBlankText
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_metric_set(self) -> "MetricSetRecord":
        refs = tuple(item.metric_ref.casefold() for item in self.definitions)
        if len(refs) != len(set(refs)):
            raise ValueError("metric set definitions must use unique metric refs")
        if self.content_digest != self.calculate_content_digest(
            tenant_id=self.tenant_id,
            source_id=self.source_id,
            source_snapshot_version=self.source_snapshot_version,
            schema_fingerprint=self.schema_fingerprint,
            domain_id=self.domain_id,
            binding_id=self.binding_id,
            binding_version=self.binding_version,
            metric_set_id=self.metric_set_id,
            version=self.version,
            definitions=self.definitions,
        ):
            raise ValueError("metric set content digest does not match its authority")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self

    @staticmethod
    def calculate_content_digest(
        *,
        tenant_id: str,
        source_id: str,
        source_snapshot_version: int,
        schema_fingerprint: str,
        domain_id: str,
        binding_id: str,
        binding_version: int,
        metric_set_id: str,
        version: int,
        definitions: tuple[SemanticMetricDefinitionV2, ...],
    ) -> str:
        return semantic_digest(
            {
                "schema_version": 1,
                "tenant_id": tenant_id,
                "source_id": source_id,
                "source_snapshot_version": source_snapshot_version,
                "schema_fingerprint": schema_fingerprint,
                "domain_id": domain_id,
                "binding_id": binding_id,
                "binding_version": binding_version,
                "metric_set_id": metric_set_id,
                "version": version,
                "definitions": definitions,
            }
        )

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        source_id: str,
        source_snapshot_version: int,
        schema_fingerprint: str,
        domain_id: str,
        binding_id: str,
        binding_version: int,
        metric_set_id: str,
        version: int,
        definitions: tuple[SemanticMetricDefinitionV2, ...],
        created_by: str,
        status: MetricSetStatus = MetricSetStatus.DRAFT,
    ) -> "MetricSetRecord":
        digest = cls.calculate_content_digest(
            tenant_id=tenant_id,
            source_id=source_id,
            source_snapshot_version=source_snapshot_version,
            schema_fingerprint=schema_fingerprint,
            domain_id=domain_id,
            binding_id=binding_id,
            binding_version=binding_version,
            metric_set_id=metric_set_id,
            version=version,
            definitions=definitions,
        )
        return cls(
            tenant_id=tenant_id,
            source_id=source_id,
            source_snapshot_version=source_snapshot_version,
            schema_fingerprint=schema_fingerprint,
            domain_id=domain_id,
            binding_id=binding_id,
            binding_version=binding_version,
            metric_set_id=metric_set_id,
            version=version,
            status=status,
            definitions=definitions,
            content_digest=digest,
            created_by=created_by,
        )


class ActiveMetricSetPointer(MetricAstModel):
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    domain_id: StableIdentifier
    binding_id: StableIdentifier
    binding_version: int = Field(ge=1)
    metric_set_id: StableIdentifier
    metric_set_version: int = Field(ge=1)
    metric_set_digest: NonBlankText
    revision: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MetricProposalCandidate(MetricAstModel):
    candidate_id: StableIdentifier
    definition: SemanticMetricDefinitionV2
    label: NonBlankText
    rationale: NonBlankText
    required_decisions: tuple[NonBlankText, ...] = ()


class MetricProposal(MetricAstModel):
    proposal_id: StableIdentifier
    revision: int = Field(default=1, ge=1)
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    source_snapshot_version: int = Field(ge=1)
    schema_fingerprint: NonBlankText
    domain_id: StableIdentifier
    base_binding_id: StableIdentifier
    base_binding_version: int = Field(ge=1)
    requested_term: NonBlankText
    status: MetricProposalStatus = MetricProposalStatus.DRAFT
    risk_tier: MetricRiskTier = MetricRiskTier.HIGH
    domain_pack: DomainPackIdentity | None = None
    candidates: tuple[MetricProposalCandidate, ...] = Field(min_length=1)
    selected_candidate_id: StableIdentifier | None = None
    created_by: NonBlankText
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_proposal(self) -> "MetricProposal":
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("metric proposal candidate ids must be unique")
        if (
            self.selected_candidate_id is not None
            and self.selected_candidate_id not in candidate_ids
        ):
            raise ValueError("selected metric candidate is unavailable")
        if self.status in {
            MetricProposalStatus.VALIDATED,
            MetricProposalStatus.PENDING_APPROVAL,
            MetricProposalStatus.APPROVED,
        } and self.selected_candidate_id is None:
            raise ValueError("validated or approved proposals require a selected candidate")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self

    @property
    def content_digest(self) -> str:
        return semantic_digest(
            {
                "tenant_id": self.tenant_id,
                "source_id": self.source_id,
                "source_snapshot_version": self.source_snapshot_version,
                "schema_fingerprint": self.schema_fingerprint,
                "domain_id": self.domain_id,
                "base_binding_id": self.base_binding_id,
                "base_binding_version": self.base_binding_version,
                "requested_term": self.requested_term,
                "domain_pack": self.domain_pack,
                "candidates": self.candidates,
                "selected_candidate_id": self.selected_candidate_id,
            }
        )


class MetricValidationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class MetricValidationIssue(MetricAstModel):
    severity: MetricValidationSeverity
    code: StableIdentifier
    message: NonBlankText
    field_refs: tuple[NonBlankText, ...] = ()


class MetricValidationReport(MetricAstModel):
    report_id: StableIdentifier
    tenant_id: StableIdentifier
    proposal_id: StableIdentifier
    proposal_revision: int = Field(ge=1)
    proposal_digest: NonBlankText
    candidate_id: StableIdentifier
    definition_digest: NonBlankText
    source_id: StableIdentifier
    source_snapshot_version: int = Field(ge=1)
    schema_fingerprint: NonBlankText
    binding_id: StableIdentifier
    binding_version: int = Field(ge=1)
    validator_digest: NonBlankText
    issues: tuple[MetricValidationIssue, ...] = ()
    preview_summary: dict[NonBlankText, MetricScalar | None] = Field(
        default_factory=dict
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def activation_allowed(self) -> bool:
        return not any(
            issue.severity == MetricValidationSeverity.ERROR for issue in self.issues
        )

    @property
    def digest(self) -> str:
        return semantic_digest(
            self.model_dump(mode="json", exclude_none=True)
        )


class MetricOverlay(MetricAstModel):
    overlay_id: StableIdentifier
    revision: int = Field(default=1, ge=1)
    tenant_id: StableIdentifier
    user_id: NonBlankText
    scope: Literal["run", "conversation"]
    run_id: NonBlankText | None = None
    conversation_id: NonBlankText | None = None
    base_context: MetricContextPin
    proposal_id: StableIdentifier
    proposal_revision: int = Field(ge=1)
    proposal_digest: NonBlankText
    definition: SemanticMetricDefinitionV2
    validation_report_id: StableIdentifier
    validation_report_digest: NonBlankText
    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def validate_overlay(self) -> "MetricOverlay":
        if self.scope == "run":
            if self.run_id is None or self.conversation_id is not None:
                raise ValueError("run overlays require only run_id")
        elif self.conversation_id is None or self.run_id is not None:
            raise ValueError("conversation overlays require only conversation_id")
        if self.expires_at <= self.created_at:
            raise ValueError("metric overlays must expire after creation")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("overlay revocation cannot precede creation")
        return self

    @property
    def content_digest(self) -> str:
        return semantic_digest(
            {
                "overlay_id": self.overlay_id,
                "revision": self.revision,
                "tenant_id": self.tenant_id,
                "user_id": self.user_id,
                "scope": self.scope,
                "run_id": self.run_id,
                "conversation_id": self.conversation_id,
                "base_context": self.base_context,
                "proposal_id": self.proposal_id,
                "proposal_revision": self.proposal_revision,
                "proposal_digest": self.proposal_digest,
                "definition": self.definition,
                "validation_report_id": self.validation_report_id,
                "validation_report_digest": self.validation_report_digest,
                "expires_at": self.expires_at,
            }
        )


class ConversationMetricPin(MetricAstModel):
    tenant_id: StableIdentifier
    user_id: NonBlankText
    conversation_id: NonBlankText
    domain_id: StableIdentifier
    context: MetricContextPin
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_pin(self) -> "ConversationMetricPin":
        if self.context.tenant_id != self.tenant_id:
            raise ValueError("conversation metric pin tenant must match context")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class DomainPackAssignment(MetricAstModel):
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    domain_id: StableIdentifier
    pack: DomainPackIdentity
    revision: int = Field(default=1, ge=1)
    enabled: bool = True
    assigned_by: NonBlankText
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_assignment(self) -> "DomainPackAssignment":
        if self.pack.domain_id != self.domain_id:
            raise ValueError("domain pack assignment domain must match pack")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class SemanticAuditEvent(MetricAstModel):
    event_id: StableIdentifier
    tenant_id: StableIdentifier
    source_id: StableIdentifier | None = None
    actor_id: NonBlankText
    action: StableIdentifier
    resource_type: StableIdentifier
    resource_id: NonBlankText
    resource_revision: int | None = Field(default=None, ge=1)
    details: dict[NonBlankText, MetricScalar | None] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "DomainPackIdentity",
    "ActiveMetricSetPointer",
    "ConversationMetricPin",
    "DomainPackAssignment",
    "MetricContextPin",
    "MetricNullPolicy",
    "MetricOverlay",
    "MetricProposal",
    "MetricProposalCandidate",
    "MetricProposalStatus",
    "MetricProvenance",
    "MetricProvenanceKind",
    "MetricRiskTier",
    "MetricScopeConvention",
    "MetricSetRecord",
    "MetricSetIdentity",
    "MetricSetStatus",
    "MetricValidationIssue",
    "MetricValidationReport",
    "MetricValidationSeverity",
    "SemanticAuditEvent",
    "SemanticMetricDefinitionV2",
]
