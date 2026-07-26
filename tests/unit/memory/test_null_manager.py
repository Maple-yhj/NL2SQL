from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from data_agent.memory.contracts import MemoryManager
from data_agent.memory.manager import (
    MemoryApprovalError,
    MemoryConflictError,
    MemoryProposalNotFoundError,
    NullMemoryManager,
)
from data_agent.memory.models import (
    ApprovalContext,
    ApprovalDecision,
    EnterpriseMemoryContent,
    EnterpriseMemoryOwner,
    MemoryBudget,
    MemoryCandidate,
    MemoryQuery,
    MemoryScope,
    ProposalStatus,
    RecordStatus,
    ConversationWriteBatch,
    ConversationMemoryContent,
    ConversationMemoryOwner,
    ConversationSummaryWrite,
    MessageRole,
    MessageWrite,
    SubjectScope,
    UserMemoryContent,
    UserMemoryOwner,
)


NOW = datetime(2026, 7, 11, 4, 0, tzinfo=UTC)


def user_candidate(value: str = "concise") -> MemoryCandidate:
    return MemoryCandidate(
        owner=UserMemoryOwner(tenant_id="tenant-a", user_id="user-a"),
        content=UserMemoryContent(
            preference_key="report_style",
            preference_value=value,
        ),
        source="explicit_user_instruction",
    )


def approval(user_id: str, *roles: str) -> ApprovalContext:
    return ApprovalContext(
        tenant_id="tenant-a",
        approver_user_id=user_id,
        roles=roles,
        decision=ApprovalDecision.APPROVE,
        decided_at=NOW,
    )


def user_query(**updates) -> MemoryQuery:
    values = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "scopes": (MemoryScope.USER,),
        "query": "report style",
        "as_of": NOW,
    }
    values.update(updates)
    return MemoryQuery(**values)


class NullMemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = NullMemoryManager(clock=lambda: NOW)

    async def test_is_protocol_complete_and_deduplicates_deterministically(self) -> None:
        self.assertIsInstance(self.manager, MemoryManager)

        first = await self.manager.propose(user_candidate())
        second = await self.manager.propose(user_candidate())

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("proposal:"))
        self.assertEqual(self.manager.proposal_count, 1)
        self.assertEqual(
            self.manager.get_proposal(first).status,
            ProposalStatus.PENDING_APPROVAL,
        )

    async def test_pending_memory_is_never_recalled_and_user_authority_is_enforced(self) -> None:
        proposal_id = await self.manager.propose(user_candidate())

        pending = await self.manager.recall(user_query(), MemoryBudget())
        self.assertEqual(pending.records, ())

        with self.assertRaises(MemoryApprovalError):
            await self.manager.commit(proposal_id, approval("user-b"))

        await self.manager.commit(proposal_id, approval("user-a"))
        recalled = await self.manager.recall(user_query(), MemoryBudget())

        self.assertEqual(len(recalled.records), 1)
        self.assertEqual(recalled.records[0].content.preference_value, "concise")
        self.assertEqual(
            self.manager.get_proposal(proposal_id).status,
            ProposalStatus.COMMITTED,
        )

    async def test_proposal_listing_enforces_owner_and_admin_visibility(self) -> None:
        own = await self.manager.propose(user_candidate())
        other = await self.manager.propose(
            user_candidate("detailed").model_copy(
                update={
                    "owner": UserMemoryOwner(
                        tenant_id="tenant-a",
                        user_id="user-b",
                    )
                }
            )
        )
        enterprise = await self.manager.propose(
            MemoryCandidate(
                owner=EnterpriseMemoryOwner(
                    tenant_id="tenant-a",
                    domain_id="commerce",
                ),
                content=EnterpriseMemoryContent(
                    category="metric_rule",
                    statement="GMV excludes cancelled orders.",
                ),
                source="curated_review",
            )
        )

        analyst_items = await self.manager.list_proposals(
            tenant_id="tenant-a",
            user_id="user-a",
            roles=("analyst",),
            statuses=(ProposalStatus.PENDING_APPROVAL,),
            limit=10,
        )
        admin_items = await self.manager.list_proposals(
            tenant_id="tenant-a",
            user_id="admin-a",
            roles=("memory_admin",),
            statuses=(ProposalStatus.PENDING_APPROVAL,),
            limit=10,
        )

        self.assertEqual(
            {item.proposal_id for item in analyst_items},
            {own},
        )
        self.assertEqual(
            {item.proposal_id for item in admin_items},
            {own, other, enterprise},
        )

    async def test_enterprise_memory_requires_admin_approval(self) -> None:
        candidate = MemoryCandidate(
            owner=EnterpriseMemoryOwner(
                tenant_id="tenant-a",
                domain_id="commerce",
            ),
            content=EnterpriseMemoryContent(
                category="metric_rule",
                statement="GMV excludes cancelled orders.",
            ),
            source="curated_review",
        )
        proposal_id = await self.manager.propose(candidate)

        with self.assertRaises(MemoryApprovalError):
            await self.manager.commit(proposal_id, approval("user-a", "analyst"))
        await self.manager.commit(proposal_id, approval("admin-a", "admin"))

        result = await self.manager.recall(
            MemoryQuery(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                scopes=(MemoryScope.ENTERPRISE,),
                query="GMV",
                as_of=NOW,
            ),
            MemoryBudget(),
        )
        self.assertEqual(len(result.records), 1)

    async def test_conflicting_slot_cannot_commit(self) -> None:
        first = await self.manager.propose(user_candidate("concise"))
        await self.manager.commit(first, approval("user-a"))

        second = await self.manager.propose(user_candidate("detailed"))

        self.assertEqual(
            self.manager.get_proposal(second).status,
            ProposalStatus.CONFLICT,
        )
        with self.assertRaises(MemoryConflictError):
            await self.manager.commit(second, approval("user-a"))

    async def test_expiry_and_version_drift_are_fail_closed(self) -> None:
        expired = user_candidate().model_copy(
            update={"expires_at": NOW - timedelta(seconds=1)}
        )
        expired_id = await self.manager.propose(expired)
        await self.manager.commit(expired_id, approval("user-a"))

        expired_result = await self.manager.recall(user_query(), MemoryBudget())
        self.assertEqual(expired_result.records, ())
        expired_record = self.manager.records[0]
        self.assertEqual(expired_record.status, RecordStatus.ACTIVE)

        versioned = user_candidate("brief").model_copy(
            update={
                "versions": {
                    "domain_version": "1.0.0",
                    "binding_version": "2.0.0",
                    "schema_fingerprint": "schema-a",
                },
                "deduplication_key": "report-style-v2",
            }
        )
        versioned_id = await self.manager.propose(versioned)
        await self.manager.commit(versioned_id, approval("user-a"))

        drifted = await self.manager.recall(
            user_query(
                versions={
                    "domain_version": "1.0.0",
                    "binding_version": "2.1.0",
                    "schema_fingerprint": "schema-a",
                }
            ),
            MemoryBudget(),
        )
        self.assertEqual(drifted.records, ())
        self.assertEqual(self.manager.records[-1].status, RecordStatus.PENDING_REVIEW)

    async def test_forget_is_owner_authorized_and_removes_subject_content(self) -> None:
        conversation = await self.manager.create_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            title="Private",
        )
        conversation_candidate = MemoryCandidate(
            owner=ConversationMemoryOwner(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
            ),
            content=ConversationMemoryContent(
                summary="Private weekly analysis context."
            ),
            source="conversation_summary",
        )
        proposal_id = await self.manager.propose(conversation_candidate)
        await self.manager.save_turn(
            ConversationWriteBatch(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
                run_id="run-a",
                user_message=MessageWrite(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    domain_id="commerce",
                    conversation_id=conversation.conversation_id,
                    run_id="run-a",
                    role=MessageRole.USER,
                    content="Remember concise reports.",
                ),
                assistant_message=MessageWrite(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    domain_id="commerce",
                    conversation_id=conversation.conversation_id,
                    run_id="run-a",
                    role=MessageRole.ASSISTANT,
                    content="Preference proposed.",
                ),
                conversation_summary=ConversationSummaryWrite(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    domain_id="commerce",
                    conversation_id=conversation.conversation_id,
                    run_id="run-a",
                    summary="Private weekly analysis context.",
                ),
                proposals=(conversation_candidate,),
            )
        )
        stranger = SubjectScope(
            tenant_id="tenant-a",
            domain_id="commerce",
            actor_user_id="user-b",
            user_id="user-a",
            conversation_id=conversation.conversation_id,
        )
        self.assertEqual(await self.manager.forget(stranger), 0)
        self.assertIsNotNone(
            await self.manager.get_conversation(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
            )
        )

        subject = stranger.model_copy(update={"actor_user_id": "user-a"})
        self.assertGreaterEqual(await self.manager.forget(subject), 4)
        self.assertIsNone(
            await self.manager.get_conversation(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
            )
        )
        self.assertEqual(
            await self.manager.list_messages(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
                limit=10,
            ),
            (),
        )
        with self.assertRaises(MemoryProposalNotFoundError):
            self.manager.get_proposal(proposal_id)


if __name__ == "__main__":
    unittest.main()
