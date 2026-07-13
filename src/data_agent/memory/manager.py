"""Deterministic local implementation of the memory authority contract."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from data_agent.execution.contracts import ExecutionCheckpoint

from .contracts import MemoryManager
from .models import (
    ApprovalContext,
    ApprovalDecision,
    ArtifactReference,
    Checkpoint,
    ConversationMemoryContent,
    ConversationRecord,
    ConversationStatus,
    ConversationWriteBatch,
    EnterpriseMemoryContent,
    EpisodicMemoryContent,
    MemoryBudget,
    MemoryBundle,
    MemoryCandidate,
    MemoryContent,
    MemoryOwner,
    MemoryProposal,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySelector,
    MessageRecord,
    ProposalId,
    ProposalStatus,
    RecordStatus,
    SubjectScope,
    UserMemoryContent,
    WorkingMemoryContent,
)
from .policy import validate_candidate_content


class MemoryStateError(RuntimeError):
    """Base for stable proposal state-machine errors."""


class MemoryProposalNotFoundError(MemoryStateError):
    pass


class MemoryApprovalError(MemoryStateError):
    pass


class MemoryConflictError(MemoryStateError):
    pass


class NullMemoryManager:
    """No-external-I/O provider with real, deterministic authority semantics.

    It is intentionally useful in tests and local/offline deployments: data is
    process-local, but ownership, approval, state transitions, expiry, version
    invalidation, budgets, checkpoints, and conversations are all enforced.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._proposals: dict[str, MemoryProposal] = {}
        self._proposal_by_digest: dict[tuple[str, str], str] = {}
        self._records: list[MemoryRecord] = []
        self._conversations: dict[tuple[str, str, str, str], ConversationRecord] = {}
        self._messages: list[MessageRecord] = []
        self._checkpoints: dict[
            tuple[str, str, str, str, str], ExecutionCheckpoint
        ] = {}
        self._artifacts: dict[str, Any] = {}
        self._conversation_sequence = 0
        self._message_sequence = 0

    @property
    def proposal_count(self) -> int:
        return len(self._proposals)

    @property
    def records(self) -> tuple[MemoryRecord, ...]:
        return tuple(self._records)

    def get_proposal(self, proposal_id: str) -> MemoryProposal:
        try:
            return self._proposals[proposal_id]
        except KeyError as exc:
            raise MemoryProposalNotFoundError("memory proposal was not found") from exc

    async def recall(
        self,
        query: MemoryQuery,
        budget: MemoryBudget,
    ) -> MemoryBundle:
        now = query.as_of
        eligible: list[MemoryRecord] = []
        for index, record in enumerate(tuple(self._records)):
            if not _query_owns_record(query, record):
                continue
            if record.status != RecordStatus.ACTIVE:
                continue
            if record.expires_at is not None and record.expires_at <= now:
                continue
            if not _versions_match(record, query):
                self._records[index] = record.model_copy(
                    update={
                        "status": RecordStatus.PENDING_REVIEW,
                        "updated_at": now,
                        "invalidation_reason": "version_drift",
                    }
                )
                continue
            if record.approval_status != ProposalStatus.COMMITTED:
                continue
            if not _text_matches(query.query, record.content):
                continue
            eligible.append(record)

        eligible.sort(key=lambda item: (item.created_at, item.memory_id), reverse=True)
        selected: list[MemoryRecord] = []
        used_characters = 0
        used_tokens = 0
        truncated = False
        for record in eligible:
            rendered = json.dumps(
                record.content.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            characters = len(rendered)
            tokens = max(1, (characters + 3) // 4)
            if (
                len(selected) >= budget.max_records
                or used_characters + characters > budget.max_characters
                or used_tokens + tokens > budget.max_tokens
            ):
                truncated = True
                continue
            selected.append(record)
            used_characters += characters
            used_tokens += tokens
        return MemoryBundle(
            records=tuple(selected),
            used_tokens=used_tokens,
            used_characters=used_characters,
            truncated=truncated,
            authority="null",
        )

    async def propose(self, candidate: MemoryCandidate) -> ProposalId:
        validate_candidate_content(candidate)
        owner_key = _owner_key(candidate.owner)
        candidate_digest = _stable_digest(candidate)
        proposal_identity = (owner_key, candidate_digest)
        existing_id = self._proposal_by_digest.get(proposal_identity)
        if existing_id is not None:
            return existing_id

        now = self._now()
        proposal_id = f"proposal:{candidate_digest[:24]}"
        deduplication_key = candidate.deduplication_key or _deduplication_key(
            candidate.owner,
            candidate.content,
        )
        conflicts = tuple(
            record.memory_id
            for record in self._records
            if record.owner_key == owner_key
            and record.deduplication_key == deduplication_key
            and record.status in {RecordStatus.ACTIVE, RecordStatus.PENDING_REVIEW}
            and record.owner == candidate.owner
            and record.content != candidate.content
        )
        status = ProposalStatus.CONFLICT if conflicts else ProposalStatus.PENDING_APPROVAL
        self._proposals[proposal_id] = MemoryProposal(
            proposal_id=proposal_id,
            owner_key=owner_key,
            candidate=candidate,
            deduplication_key=deduplication_key,
            status=status,
            proposed_at=now,
            conflict_with=conflicts,
            updated_at=now,
        )
        self._proposal_by_digest[proposal_identity] = proposal_id
        return proposal_id

    async def commit(
        self,
        proposal_id: str,
        approval: ApprovalContext,
    ) -> None:
        proposal = self._proposals.get(proposal_id)
        safe_error = "proposal decision is not authorized or unavailable"
        if proposal is None or not approval.authorizes(
            proposal.candidate.owner,
            proposal.candidate.content,
        ):
            raise MemoryApprovalError(safe_error)
        if proposal.status in {ProposalStatus.COMMITTED, ProposalStatus.REJECTED}:
            if proposal.approval is not None and _same_approval_decision(
                proposal.approval,
                approval,
            ):
                return
            raise MemoryApprovalError(safe_error)
        if proposal.status == ProposalStatus.CONFLICT:
            raise MemoryConflictError("conflicting memory proposal cannot be committed")
        if proposal.status != ProposalStatus.PENDING_APPROVAL:
            raise MemoryStateError(
                f"proposal cannot be committed from {proposal.status.value}"
            )
        if approval.decision == ApprovalDecision.REJECT:
            self._proposals[proposal_id] = proposal.model_copy(
                update={
                    "status": ProposalStatus.REJECTED,
                    "approval": approval,
                    "updated_at": approval.decided_at,
                }
            )
            return
        existing_slot = next(
            (
                record
                for record in self._records
                if record.owner_key == proposal.owner_key
                and record.deduplication_key == proposal.deduplication_key
                and record.status
                in {RecordStatus.ACTIVE, RecordStatus.PENDING_REVIEW}
            ),
            None,
        )
        if existing_slot is not None:
            if existing_slot.content != proposal.candidate.content:
                self._proposals[proposal_id] = proposal.model_copy(
                    update={
                        "status": ProposalStatus.CONFLICT,
                        "approval": approval,
                        "conflict_with": (existing_slot.memory_id,),
                        "updated_at": approval.decided_at,
                    }
                )
                raise MemoryConflictError(
                    "conflicting memory proposal cannot be committed"
                )
            self._proposals[proposal_id] = proposal.model_copy(
                update={
                    "status": ProposalStatus.COMMITTED,
                    "approval": approval,
                    "committed_memory_id": existing_slot.memory_id,
                    "updated_at": approval.decided_at,
                }
            )
            return
        memory_id = f"memory:{_stable_digest({'proposal_id': proposal_id})[:24]}"
        now = self._now()
        record = MemoryRecord(
            memory_id=memory_id,
            owner_key=proposal.owner_key,
            owner=proposal.candidate.owner,
            content=proposal.candidate.content,
            source=proposal.candidate.source,
            evidence=proposal.candidate.evidence,
            trust_level=proposal.candidate.trust_level,
            approval_status=ProposalStatus.COMMITTED,
            status=RecordStatus.ACTIVE,
            sensitivity=proposal.candidate.sensitivity,
            created_at=now,
            updated_at=now,
            expires_at=proposal.candidate.expires_at,
            versions=proposal.candidate.versions,
            deduplication_key=proposal.deduplication_key,
            proposal_id=proposal_id,
        )
        self._records.append(record)
        self._proposals[proposal_id] = proposal.model_copy(
            update={
                "status": ProposalStatus.COMMITTED,
                "approval": approval,
                "committed_memory_id": memory_id,
                "updated_at": now,
            }
        )

    async def invalidate(self, selector: MemorySelector) -> int:
        count = 0
        now = self._now()
        for index, record in enumerate(tuple(self._records)):
            if record.status not in {RecordStatus.ACTIVE, RecordStatus.PENDING_REVIEW}:
                continue
            if not _selector_matches(selector, record):
                continue
            if not _actor_may_change(
                actor_user_id=selector.actor_user_id,
                actor_roles=selector.actor_roles,
                owner=record.owner,
            ):
                continue
            self._records[index] = record.model_copy(
                update={
                    "status": RecordStatus.INVALIDATED,
                    "updated_at": now,
                    "invalidated_at": now,
                    "invalidation_reason": selector.reason,
                }
            )
            count += 1
        return count

    async def forget(self, subject: SubjectScope) -> int:
        if not _subject_authorized(subject):
            return 0
        before = (
            len(self._records)
            + len(self._messages)
            + len(self._checkpoints)
            + len(self._artifacts)
            + len(self._conversations)
            + len(self._proposals)
        )
        self._records = [
            record
            for record in self._records
            if not _subject_owns(subject, record.owner)
        ]
        self._messages = [
            message
            for message in self._messages
            if not (
                message.tenant_id == subject.tenant_id
                and message.domain_id == subject.domain_id
                and message.user_id == subject.user_id
                and (
                    subject.conversation_id is None
                    or message.conversation_id == subject.conversation_id
                )
                and (subject.run_id is None or message.run_id == subject.run_id)
            )
        ]
        self._checkpoints = {
            key: checkpoint
            for key, checkpoint in self._checkpoints.items()
            if not (
                key[0] == subject.tenant_id
                and key[2] == subject.domain_id
                and key[1] == subject.user_id
                and (subject.conversation_id is None or key[3] == subject.conversation_id)
                and (subject.run_id is None or key[4] == subject.run_id)
            )
        }
        self._artifacts = {
            artifact_id: reference
            for artifact_id, reference in self._artifacts.items()
            if not (
                reference.tenant_id == subject.tenant_id
                and reference.domain_id == subject.domain_id
                and reference.user_id == subject.user_id
                and (
                    subject.conversation_id is None
                    or reference.conversation_id == subject.conversation_id
                )
                and (subject.run_id is None or reference.run_id == subject.run_id)
            )
        }
        if subject.run_id is not None:
            conversation_key = (
                subject.tenant_id,
                subject.user_id,
                subject.domain_id,
                subject.conversation_id,
            )
            conversation = self._conversations.get(conversation_key)
            if (
                conversation is not None
                and conversation.summary_run_id == subject.run_id
            ):
                self._conversations[conversation_key] = conversation.model_copy(
                    update={
                        "summary": "",
                        "summary_run_id": None,
                        "updated_at": self._now(),
                    }
                )
        self._conversations = {
            key: conversation
            for key, conversation in self._conversations.items()
            if not (
                key[0] == subject.tenant_id
                and key[2] == subject.domain_id
                and key[1] == subject.user_id
                and subject.run_id is None
                and (subject.conversation_id is None or key[3] == subject.conversation_id)
            )
        }
        removed_proposal_ids = {
            proposal_id
            for proposal_id, proposal in self._proposals.items()
            if _subject_owns(subject, proposal.candidate.owner)
        }
        self._proposals = {
            proposal_id: proposal
            for proposal_id, proposal in self._proposals.items()
            if proposal_id not in removed_proposal_ids
        }
        self._proposal_by_digest = {
            digest: proposal_id
            for digest, proposal_id in self._proposal_by_digest.items()
            if proposal_id not in removed_proposal_ids
        }
        after = (
            len(self._records)
            + len(self._messages)
            + len(self._checkpoints)
            + len(self._artifacts)
            + len(self._conversations)
            + len(self._proposals)
        )
        return before - after

    async def save_checkpoint(self, run_id: str, state: Checkpoint) -> None:
        if run_id != state.run_id or state.checkpoint.state.run_id != run_id:
            raise ValueError("checkpoint run_id does not match its owner")
        self._checkpoints[
            (
                state.tenant_id,
                state.user_id,
                state.domain_id,
                state.conversation_id,
                state.run_id,
            )
        ] = state.checkpoint

    async def load_checkpoint(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
        run_id: str,
    ) -> ExecutionCheckpoint | None:
        return self._checkpoints.get(
            (tenant_id, user_id, domain_id, conversation_id, run_id)
        )

    async def create_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        title: str = "",
    ) -> ConversationRecord:
        self._conversation_sequence += 1
        now = self._now()
        conversation_id = "conversation:" + _stable_digest(
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "domain_id": domain_id,
                "sequence": self._conversation_sequence,
            }
        )[:24]
        record = ConversationRecord(
            tenant_id=tenant_id,
            user_id=user_id,
            domain_id=domain_id,
            conversation_id=conversation_id,
            title=title,
            status=ConversationStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self._conversations[(tenant_id, user_id, domain_id, conversation_id)] = record
        return record

    async def get_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
    ) -> ConversationRecord | None:
        return self._conversations.get(
            (tenant_id, user_id, domain_id, conversation_id)
        )

    async def list_conversations(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        limit: int,
        include_archived: bool = False,
    ) -> tuple[ConversationRecord, ...]:
        if limit <= 0:
            return ()
        matches = (
            conversation
            for conversation in self._conversations.values()
            if conversation.tenant_id == tenant_id
            and conversation.user_id == user_id
            and conversation.domain_id == domain_id
            and (
                include_archived
                or conversation.status == ConversationStatus.ACTIVE
            )
        )
        return tuple(
            sorted(
                matches,
                key=lambda item: (item.updated_at, item.conversation_id),
                reverse=True,
            )[:limit]
        )

    async def update_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationRecord | None:
        key = (tenant_id, user_id, domain_id, conversation_id)
        conversation = self._conversations.get(key)
        if conversation is None:
            return None
        updates: dict[str, object] = {"updated_at": self._now()}
        if title is not None:
            updates["title"] = title
        if archived is not None:
            updates["status"] = (
                ConversationStatus.ARCHIVED
                if archived
                else ConversationStatus.ACTIVE
            )
        updated = conversation.model_copy(update=updates)
        self._conversations[key] = updated
        return updated

    async def list_messages(
        self,
        *,
        tenant_id: str,
        user_id: str,
        domain_id: str,
        conversation_id: str,
        limit: int,
    ) -> tuple[MessageRecord, ...]:
        if (
            tenant_id,
            user_id,
            domain_id,
            conversation_id,
        ) not in self._conversations:
            return ()
        matches = tuple(
            message
            for message in self._messages
            if message.tenant_id == tenant_id
            and message.user_id == user_id
            and message.domain_id == domain_id
            and message.conversation_id == conversation_id
        )
        return matches[-max(limit, 0) :] if limit > 0 else ()

    async def save_turn(self, batch: ConversationWriteBatch) -> None:
        _ensure_batch_owner_closure(batch)
        key = (
            batch.tenant_id,
            batch.user_id,
            batch.domain_id,
            batch.conversation_id,
        )
        conversation = self._conversations.get(key)
        if conversation is None:
            raise PermissionError("conversation owner does not match")
        validate_candidate_content(batch.user_message)
        validate_candidate_content(batch.assistant_message)
        validate_candidate_content(batch.conversation_summary)
        for proposal in batch.proposals:
            validate_candidate_content(proposal)
        now = self._now()
        staged: list[MessageRecord] = []
        for message in (batch.user_message, batch.assistant_message):
            self._message_sequence += 1
            staged.append(
                MessageRecord(
                    message_id=f"message:{self._message_sequence:016x}",
                    tenant_id=batch.tenant_id,
                    user_id=batch.user_id,
                    domain_id=batch.domain_id,
                    conversation_id=batch.conversation_id,
                    run_id=batch.run_id,
                    role=message.role,
                    content=message.content,
                    payload=message.payload,
                    created_at=now,
                )
            )
        self._messages.extend(staged)
        self._conversations[key] = conversation.model_copy(
            update={
                "summary": batch.conversation_summary.summary,
                "summary_run_id": batch.run_id,
                "updated_at": now,
            }
        )
        for reference in _batch_artifact_references(batch):
            self._artifacts[reference.artifact_id] = reference
        for candidate in batch.proposals:
            await self.propose(candidate)
        if batch.checkpoint is not None:
            await self.save_checkpoint(
                batch.run_id,
                batch.checkpoint,
            )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("memory clock must return a timezone-aware datetime")
        return value


def _stable_digest(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _owner_key(owner: MemoryOwner) -> str:
    return "owner:" + _stable_digest(owner.model_dump(mode="json"))


def _deduplication_key(owner: MemoryOwner, content: MemoryContent) -> str:
    if isinstance(content, UserMemoryContent):
        slot: Any = {"preference_key": content.preference_key.casefold()}
    elif isinstance(content, EnterpriseMemoryContent):
        slot = {"category": content.category.casefold()}
    elif isinstance(content, EpisodicMemoryContent):
        slot = {"event": content.event.casefold()}
    else:
        slot = {"scope": content.scope.value}
    return "dedup:" + _stable_digest(slot)[:32]


def _same_approval_decision(
    existing: ApprovalContext,
    current: ApprovalContext,
) -> bool:
    return (
        existing.tenant_id == current.tenant_id
        and existing.approver_user_id == current.approver_user_id
        and {role.casefold() for role in existing.roles}
        == {role.casefold() for role in current.roles}
        and existing.decision == current.decision
    )


def _turn_owner_tuple(value: Any) -> tuple[str, str, str, str, str]:
    return (
        value.tenant_id,
        value.user_id,
        value.domain_id,
        value.conversation_id,
        value.run_id,
    )


def _proposal_owner_matches_turn(
    owner: MemoryOwner,
    turn_owner: tuple[str, str, str, str, str],
) -> bool:
    tenant_id, user_id, domain_id, conversation_id, run_id = turn_owner
    if owner.tenant_id != tenant_id:
        return False
    expected = {
        "user_id": user_id,
        "domain_id": domain_id,
        "conversation_id": conversation_id,
        "run_id": run_id,
    }
    return all(
        not hasattr(owner, field_name)
        or getattr(owner, field_name) == expected_value
        for field_name, expected_value in expected.items()
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


def _batch_artifact_references(
    batch: ConversationWriteBatch,
) -> tuple[ArtifactReference, ...]:
    references: list[ArtifactReference] = [*batch.artifact_refs]
    references.extend(batch.user_message.payload.artifact_refs)
    references.extend(batch.assistant_message.payload.artifact_refs)
    for candidate in batch.proposals:
        references.extend(_candidate_artifact_references(candidate))
    unique: dict[str, ArtifactReference] = {}
    for reference in references:
        unique[reference.artifact_id] = reference
    return tuple(unique.values())


def _ensure_batch_owner_closure(batch: ConversationWriteBatch) -> None:
    expected = (
        batch.tenant_id,
        batch.user_id,
        batch.domain_id,
        batch.conversation_id,
        batch.run_id,
    )
    owned_items = (
        batch.user_message,
        batch.assistant_message,
        batch.conversation_summary,
    )
    if any(_turn_owner_tuple(item) != expected for item in owned_items):
        raise PermissionError("turn component owner does not match")
    if batch.checkpoint is not None and _turn_owner_tuple(batch.checkpoint) != expected:
        raise PermissionError("checkpoint owner does not match")
    for candidate in batch.proposals:
        if not _proposal_owner_matches_turn(candidate.owner, expected):
            raise PermissionError("proposal owner does not match")
    if any(
        _turn_owner_tuple(reference) != expected
        for reference in _batch_artifact_references(batch)
    ):
        raise PermissionError("artifact reference owner does not match")


def _content_text(content: MemoryContent) -> str:
    if isinstance(content, UserMemoryContent):
        return f"{content.preference_key} {content.preference_value}"
    if isinstance(content, EnterpriseMemoryContent):
        return f"{content.category} {content.statement}"
    if isinstance(content, EpisodicMemoryContent):
        return f"{content.event} {content.lesson} {content.outcome}"
    return str(getattr(content, "summary", ""))


def _text_matches(query: str, content: MemoryContent) -> bool:
    terms = {term.casefold() for term in query.split() if term.strip()}
    if not terms:
        return True
    haystack = _content_text(content).casefold()
    return any(term in haystack for term in terms)


def _query_owns_record(query: MemoryQuery, record: MemoryRecord) -> bool:
    owner = record.owner
    if owner.tenant_id != query.tenant_id or record.scope not in query.scopes:
        return False
    if owner.scope in {
        MemoryScope.WORKING,
        MemoryScope.CONVERSATION,
        MemoryScope.USER,
    } and getattr(owner, "user_id", None) != query.user_id:
        return False
    if owner.scope in {MemoryScope.WORKING, MemoryScope.CONVERSATION} and getattr(
        owner, "conversation_id", None
    ) != query.conversation_id:
        return False
    if owner.scope == MemoryScope.WORKING and getattr(owner, "run_id", None) != query.run_id:
        return False
    if owner.scope in {
        MemoryScope.WORKING,
        MemoryScope.CONVERSATION,
        MemoryScope.EPISODIC,
        MemoryScope.ENTERPRISE,
    } and getattr(
        owner, "domain_id", None
    ) != query.domain_id:
        return False
    return True


def _versions_match(record: MemoryRecord, query: MemoryQuery) -> bool:
    for field_name in ("domain_version", "binding_version", "schema_fingerprint"):
        expected = getattr(record.versions, field_name)
        if expected is not None and getattr(query.versions, field_name) != expected:
            return False
    return True


def _is_admin(roles: tuple[str, ...]) -> bool:
    normalized = {role.casefold() for role in roles}
    return bool(normalized & {"admin", "memory_admin", "enterprise_admin"})


def _actor_may_change(
    *,
    actor_user_id: str,
    actor_roles: tuple[str, ...],
    owner: MemoryOwner,
) -> bool:
    if _is_admin(actor_roles):
        return True
    if owner.scope in {MemoryScope.EPISODIC, MemoryScope.ENTERPRISE}:
        return False
    return getattr(owner, "user_id", None) == actor_user_id


def _selector_matches(selector: MemorySelector, record: MemoryRecord) -> bool:
    owner = record.owner
    if owner.tenant_id != selector.tenant_id:
        return False
    if selector.memory_ids and record.memory_id not in selector.memory_ids:
        return False
    if selector.scopes and record.scope not in selector.scopes:
        return False
    for selector_field in ("user_id", "domain_id", "conversation_id", "run_id"):
        selected = getattr(selector, selector_field)
        if selected is not None and getattr(owner, selector_field, None) != selected:
            return False
    return True


def _subject_authorized(subject: SubjectScope) -> bool:
    return _is_admin(subject.actor_roles) or subject.actor_user_id == subject.user_id


def _subject_owns(subject: SubjectScope, owner: MemoryOwner) -> bool:
    if owner.tenant_id != subject.tenant_id:
        return False
    if getattr(owner, "domain_id", None) != subject.domain_id:
        return False
    if getattr(owner, "user_id", None) != subject.user_id:
        return False
    if subject.conversation_id is not None and getattr(
        owner, "conversation_id", None
    ) != subject.conversation_id:
        return False
    if subject.run_id is not None and getattr(owner, "run_id", None) != subject.run_id:
        return False
    return True


assert isinstance(NullMemoryManager(), MemoryManager)


__all__ = [
    "MemoryApprovalError",
    "MemoryConflictError",
    "MemoryProposalNotFoundError",
    "MemoryStateError",
    "NullMemoryManager",
]
