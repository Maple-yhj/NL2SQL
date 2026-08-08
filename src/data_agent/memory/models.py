"""Frozen, scope-safe contracts for Data Agent memory and conversations."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)



NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class MemoryModel(BaseModel):
    """Exact-field, immutable base that also validates ``model_copy`` updates."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    def model_copy(
        self,
        *,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        unknown = set(update) - set(type(self).model_fields)
        if unknown:
            raise ValueError(
                "model_copy update contains unknown fields: "
                + ", ".join(sorted(unknown))
            )
        values = {
            name: deepcopy(getattr(self, name)) if deep else getattr(self, name)
            for name in type(self).model_fields
        }
        values.update(deepcopy(update) if deep else update)
        return type(self).model_validate(values)


class MemoryScope(StrEnum):
    WORKING = "working"
    CONVERSATION = "conversation"
    USER = "user"
    EPISODIC = "episodic"
    ENTERPRISE = "enterprise"


class TrustLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERIFIED = "verified"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    POLICY_REJECTED = "policy_rejected"
    COMMITTED = "committed"
    INVALIDATED = "invalidated"


class RecordStatus(StrEnum):
    ACTIVE = "active"
    PENDING_REVIEW = "pending_review"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class WorkingMemoryOwner(MemoryModel):
    scope: Literal[MemoryScope.WORKING] = MemoryScope.WORKING
    tenant_id: NonBlankText
    user_id: NonBlankText
    domain_id: NonBlankText
    conversation_id: NonBlankText
    run_id: NonBlankText


class ConversationMemoryOwner(MemoryModel):
    scope: Literal[MemoryScope.CONVERSATION] = MemoryScope.CONVERSATION
    tenant_id: NonBlankText
    user_id: NonBlankText
    domain_id: NonBlankText
    conversation_id: NonBlankText


class UserMemoryOwner(MemoryModel):
    scope: Literal[MemoryScope.USER] = MemoryScope.USER
    tenant_id: NonBlankText
    user_id: NonBlankText


class EpisodicMemoryOwner(MemoryModel):
    scope: Literal[MemoryScope.EPISODIC] = MemoryScope.EPISODIC
    tenant_id: NonBlankText
    domain_id: NonBlankText


class EnterpriseMemoryOwner(MemoryModel):
    scope: Literal[MemoryScope.ENTERPRISE] = MemoryScope.ENTERPRISE
    tenant_id: NonBlankText
    domain_id: NonBlankText


MemoryOwner = Annotated[
    WorkingMemoryOwner
    | ConversationMemoryOwner
    | UserMemoryOwner
    | EpisodicMemoryOwner
    | EnterpriseMemoryOwner,
    Field(discriminator="scope"),
]


class ArtifactReference(MemoryModel):
    """A pointer to a complete artifact; the artifact body never lives in memory."""

    artifact_id: NonBlankText
    tenant_id: NonBlankText
    user_id: NonBlankText
    domain_id: NonBlankText
    conversation_id: NonBlankText
    run_id: NonBlankText
    kind: NonBlankText
    digest: Digest
    row_count: int | None = Field(default=None, ge=0)


class WorkingMemoryContent(MemoryModel):
    scope: Literal[MemoryScope.WORKING] = MemoryScope.WORKING
    summary: NonBlankText
    artifact_refs: tuple[ArtifactReference, ...] = ()


class ConversationMemoryContent(MemoryModel):
    scope: Literal[MemoryScope.CONVERSATION] = MemoryScope.CONVERSATION
    summary: NonBlankText
    topics: tuple[NonBlankText, ...] = ()
    artifact_refs: tuple[ArtifactReference, ...] = ()


class UserMemoryContent(MemoryModel):
    scope: Literal[MemoryScope.USER] = MemoryScope.USER
    preference_key: NonBlankText
    preference_value: NonBlankText


class EpisodicMemoryContent(MemoryModel):
    scope: Literal[MemoryScope.EPISODIC] = MemoryScope.EPISODIC
    event: NonBlankText
    lesson: NonBlankText
    outcome: NonBlankText
    artifact_refs: tuple[ArtifactReference, ...] = ()


class EnterpriseMemoryContent(MemoryModel):
    scope: Literal[MemoryScope.ENTERPRISE] = MemoryScope.ENTERPRISE
    category: NonBlankText
    statement: NonBlankText
    evidence_refs: tuple[ArtifactReference, ...] = ()


MemoryContent = Annotated[
    WorkingMemoryContent
    | ConversationMemoryContent
    | UserMemoryContent
    | EpisodicMemoryContent
    | EnterpriseMemoryContent,
    Field(discriminator="scope"),
]


class MemoryVersionPins(MemoryModel):
    domain_version: NonBlankText | None = None
    binding_version: NonBlankText | None = None
    schema_fingerprint: NonBlankText | None = None


class MemoryEvidence(MemoryModel):
    summary: NonBlankText
    artifact_refs: tuple[ArtifactReference, ...] = ()
    row_count: int | None = Field(default=None, ge=0)


class MemoryCandidate(MemoryModel):
    owner: MemoryOwner
    content: MemoryContent
    source: NonBlankText
    evidence: MemoryEvidence | None = None
    trust_level: TrustLevel = TrustLevel.MEDIUM
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    expires_at: datetime | None = None
    versions: MemoryVersionPins = Field(default_factory=MemoryVersionPins)
    deduplication_key: NonBlankText | None = None

    @property
    def scope(self) -> MemoryScope:
        return MemoryScope(self.owner.scope)

    @model_validator(mode="after")
    def validate_scopes_and_expiry(self) -> "MemoryCandidate":
        if self.owner.scope != self.content.scope:
            raise ValueError("owner and content scopes must match")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return self


class MemoryRecord(MemoryModel):
    memory_id: NonBlankText
    owner_key: NonBlankText
    owner: MemoryOwner
    content: MemoryContent
    source: NonBlankText
    evidence: MemoryEvidence | None = None
    trust_level: TrustLevel = TrustLevel.MEDIUM
    approval_status: ProposalStatus = ProposalStatus.COMMITTED
    status: RecordStatus = RecordStatus.ACTIVE
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    versions: MemoryVersionPins = Field(default_factory=MemoryVersionPins)
    deduplication_key: NonBlankText
    proposal_id: NonBlankText
    invalidated_at: datetime | None = None
    invalidation_reason: str | None = None

    @property
    def scope(self) -> MemoryScope:
        return MemoryScope(self.owner.scope)

    @model_validator(mode="after")
    def validate_record(self) -> "MemoryRecord":
        if self.owner.scope != self.content.scope:
            raise ValueError("owner and content scopes must match")
        for name in ("created_at", "updated_at", "expires_at", "invalidated_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        return self


class ApprovalContext(MemoryModel):
    tenant_id: NonBlankText
    approver_user_id: NonBlankText
    roles: tuple[NonBlankText, ...] = ()
    decision: ApprovalDecision
    decided_at: datetime
    reason: str | None = None

    @model_validator(mode="after")
    def validate_decision_time(self) -> "ApprovalContext":
        if self.decided_at.tzinfo is None:
            raise ValueError("decided_at must be timezone-aware")
        return self

    def authorizes(self, owner: MemoryOwner, content: MemoryContent) -> bool:
        if (
            self.tenant_id != owner.tenant_id
            or owner.scope != content.scope
        ):
            return False
        roles = {role.casefold() for role in self.roles}
        is_admin = bool(roles & {"admin", "memory_admin", "enterprise_admin"})
        if owner.scope in {MemoryScope.ENTERPRISE, MemoryScope.EPISODIC}:
            return is_admin
        owner_user_id = getattr(owner, "user_id", None)
        return is_admin or self.approver_user_id == owner_user_id


class MemoryProposal(MemoryModel):
    proposal_id: NonBlankText
    owner_key: NonBlankText
    candidate: MemoryCandidate
    deduplication_key: NonBlankText
    status: ProposalStatus = ProposalStatus.PENDING_APPROVAL
    proposed_at: datetime
    proposed_by: NonBlankText | None = None
    approval: ApprovalContext | None = None
    conflict_with: tuple[NonBlankText, ...] = ()
    committed_memory_id: NonBlankText | None = None
    updated_at: datetime


class MemoryQuery(MemoryModel):
    tenant_id: NonBlankText
    user_id: NonBlankText
    domain_id: NonBlankText | None = None
    conversation_id: NonBlankText | None = None
    run_id: NonBlankText | None = None
    scopes: tuple[MemoryScope, ...] = (
        MemoryScope.CONVERSATION,
        MemoryScope.USER,
        MemoryScope.EPISODIC,
        MemoryScope.ENTERPRISE,
    )
    query: str = ""
    versions: MemoryVersionPins = Field(default_factory=MemoryVersionPins)
    as_of: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_query_scope(self) -> "MemoryQuery":
        if not self.scopes or len(self.scopes) != len(set(self.scopes)):
            raise ValueError("query scopes must be non-empty and unique")
        if MemoryScope.WORKING in self.scopes and (
            self.conversation_id is None or self.run_id is None
        ):
            raise ValueError("working recall requires conversation_id and run_id")
        if MemoryScope.CONVERSATION in self.scopes and self.conversation_id is None:
            raise ValueError("conversation recall requires conversation_id")
        if any(
            scope in self.scopes
            for scope in (MemoryScope.WORKING, MemoryScope.CONVERSATION)
        ) and self.domain_id is None:
            raise ValueError("conversation-scoped recall requires domain_id")
        if any(
            scope in self.scopes
            for scope in (MemoryScope.EPISODIC, MemoryScope.ENTERPRISE)
        ) and self.domain_id is None:
            raise ValueError("domain recall requires domain_id")
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return self


class MemoryBudget(MemoryModel):
    max_records: int = Field(default=20, ge=1, le=100)
    max_tokens: int = Field(default=2048, ge=16, le=32768)
    max_characters: int = Field(default=8192, ge=64, le=131072)

    @model_validator(mode="after")
    def validate_caps(self) -> "MemoryBudget":
        if self.max_characters < self.max_tokens:
            raise ValueError("max_characters cannot be smaller than max_tokens")
        return self


class MemoryBundle(MemoryModel):
    records: tuple[MemoryRecord, ...] = ()
    used_tokens: int = Field(default=0, ge=0)
    used_characters: int = Field(default=0, ge=0)
    truncated: bool = False
    authority: Literal["postgres", "null"] = "postgres"


class MemorySelector(MemoryModel):
    tenant_id: NonBlankText
    actor_user_id: NonBlankText
    actor_roles: tuple[NonBlankText, ...] = ()
    memory_ids: tuple[NonBlankText, ...] = ()
    scopes: tuple[MemoryScope, ...] = ()
    user_id: NonBlankText | None = None
    domain_id: NonBlankText | None = None
    conversation_id: NonBlankText | None = None
    run_id: NonBlankText | None = None
    reason: NonBlankText = "invalidated"


class SubjectScope(MemoryModel):
    tenant_id: NonBlankText
    domain_id: NonBlankText
    actor_user_id: NonBlankText
    actor_roles: tuple[NonBlankText, ...] = ()
    user_id: NonBlankText
    conversation_id: NonBlankText | None = None
    run_id: NonBlankText | None = None

    @model_validator(mode="after")
    def validate_run_scope(self) -> "SubjectScope":
        if self.run_id is not None and self.conversation_id is None:
            raise ValueError("run-scoped forget requires conversation_id")
        return self


class ConversationRecord(MemoryModel):
    tenant_id: NonBlankText
    user_id: NonBlankText
    domain_id: NonBlankText
    conversation_id: NonBlankText
    title: str = ""
    summary: str = ""
    summary_run_id: NonBlankText | None = None
    status: ConversationStatus = ConversationStatus.ACTIVE
    created_at: datetime
    updated_at: datetime


class TraceSummary(MemoryModel):
    node: NonBlankText
    status: NonBlankText
    error_code: str | None = None


class SafeMessagePayload(MemoryModel):
    message_type: NonBlankText = "text"
    contextualized_question: str | None = None
    answer_summary: str | None = None
    ok: bool | None = None
    error_code: str | None = None
    sql_digest: Digest | None = None
    row_count: int | None = Field(default=None, ge=0)
    artifact_refs: tuple[ArtifactReference, ...] = ()
    trace: tuple[TraceSummary, ...] = ()


class MessageRecord(MemoryModel):
    message_id: NonBlankText
    tenant_id: NonBlankText
    user_id: NonBlankText
    domain_id: NonBlankText
    conversation_id: NonBlankText
    run_id: NonBlankText
    role: MessageRole
    content: str
    payload: SafeMessagePayload = Field(default_factory=SafeMessagePayload)
    created_at: datetime


class MessageWrite(MemoryModel):
    tenant_id: NonBlankText
    user_id: NonBlankText
    domain_id: NonBlankText
    conversation_id: NonBlankText
    run_id: NonBlankText
    role: MessageRole
    content: str
    payload: SafeMessagePayload = Field(default_factory=SafeMessagePayload)


class ConversationSummaryWrite(MemoryModel):
    tenant_id: NonBlankText
    user_id: NonBlankText
    domain_id: NonBlankText
    conversation_id: NonBlankText
    run_id: NonBlankText
    summary: str = ""


class ConversationWriteBatch(MemoryModel):
    tenant_id: NonBlankText
    user_id: NonBlankText
    domain_id: NonBlankText
    conversation_id: NonBlankText
    run_id: NonBlankText
    user_message: MessageWrite
    assistant_message: MessageWrite
    conversation_summary: ConversationSummaryWrite
    artifact_refs: tuple[ArtifactReference, ...] = ()
    proposals: tuple[MemoryCandidate, ...] = ()

    @model_validator(mode="after")
    def validate_batch_owners(self) -> "ConversationWriteBatch":
        if self.user_message.role != MessageRole.USER:
            raise ValueError("user_message must have the user role")
        if self.assistant_message.role != MessageRole.ASSISTANT:
            raise ValueError("assistant_message must have the assistant role")
        owner = (
            self.tenant_id,
            self.user_id,
            self.domain_id,
            self.conversation_id,
            self.run_id,
        )
        for name, item in (
            ("user message", self.user_message),
            ("assistant message", self.assistant_message),
            ("conversation summary", self.conversation_summary),
        ):
            if _turn_owner_tuple(item) != owner:
                raise ValueError(f"{name} owner does not match the turn")
        references = [*self.artifact_refs]
        references.extend(self.user_message.payload.artifact_refs)
        references.extend(self.assistant_message.payload.artifact_refs)
        for proposal in self.proposals:
            references.extend(_candidate_artifact_references(proposal))
        for reference in references:
            if (
                reference.tenant_id,
                reference.user_id,
                reference.domain_id,
                reference.conversation_id,
                reference.run_id,
            ) != owner:
                raise ValueError("artifact reference owner does not match the turn")
        for proposal in self.proposals:
            if not _proposal_owner_matches_turn(proposal.owner, owner):
                raise ValueError("proposal owner does not match the turn")
        return self


def _turn_owner_tuple(value: Any) -> tuple[str, str, str, str, str]:
    return (
        value.tenant_id,
        value.user_id,
        value.domain_id,
        value.conversation_id,
        value.run_id,
    )


def _proposal_owner_matches_turn(
    proposal_owner: MemoryOwner,
    turn_owner: tuple[str, str, str, str, str],
) -> bool:
    tenant_id, user_id, domain_id, conversation_id, run_id = turn_owner
    if proposal_owner.tenant_id != tenant_id:
        return False
    expected = {
        "user_id": user_id,
        "domain_id": domain_id,
        "conversation_id": conversation_id,
        "run_id": run_id,
    }
    return all(
        not hasattr(proposal_owner, field_name)
        or getattr(proposal_owner, field_name) == value
        for field_name, value in expected.items()
    )


def _candidate_artifact_references(
    candidate: MemoryCandidate,
) -> tuple[ArtifactReference, ...]:
    references: list[ArtifactReference] = []
    if candidate.evidence is not None:
        references.extend(candidate.evidence.artifact_refs)
    content = candidate.content
    if isinstance(content, EnterpriseMemoryContent):
        references.extend(content.evidence_refs)
    elif isinstance(
        content,
        (
            WorkingMemoryContent,
            ConversationMemoryContent,
            EpisodicMemoryContent,
        ),
    ):
        references.extend(content.artifact_refs)
    return tuple(references)


ProposalId = NonBlankText


__all__ = [
    "ApprovalContext",
    "ApprovalDecision",
    "ArtifactReference",
    "ConversationMemoryContent",
    "ConversationMemoryOwner",
    "ConversationRecord",
    "ConversationSummaryWrite",
    "ConversationStatus",
    "ConversationWriteBatch",
    "Digest",
    "EnterpriseMemoryContent",
    "EnterpriseMemoryOwner",
    "EpisodicMemoryContent",
    "EpisodicMemoryOwner",
    "MemoryBudget",
    "MemoryBundle",
    "MemoryCandidate",
    "MemoryContent",
    "MemoryEvidence",
    "MemoryModel",
    "MemoryOwner",
    "MemoryProposal",
    "MemoryQuery",
    "MemoryRecord",
    "MemoryScope",
    "MemorySelector",
    "MemoryVersionPins",
    "MessageRecord",
    "MessageRole",
    "MessageWrite",
    "NonBlankText",
    "ProposalId",
    "ProposalStatus",
    "RecordStatus",
    "SafeMessagePayload",
    "Sensitivity",
    "SubjectScope",
    "TraceSummary",
    "TrustLevel",
    "UserMemoryContent",
    "UserMemoryOwner",
    "WorkingMemoryContent",
    "WorkingMemoryOwner",
]
