from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from data_agent.memory.manager import (
    MemoryApprovalError,
    MemoryConflictError,
    MemoryProposalNotFoundError,
    NullMemoryManager,
)
from data_agent.memory.models import (
    ApprovalContext,
    ApprovalDecision,
    ConversationRecord,
    ConversationSummaryWrite,
    ConversationWriteBatch,
    MemoryBudget,
    MemoryCandidate,
    MemoryQuery,
    MemoryScope,
    MessageRole,
    MessageWrite,
    ProposalStatus,
    SubjectScope,
    UserMemoryContent,
    UserMemoryOwner,
    WorkingMemoryContent,
    WorkingMemoryOwner,
)


NOW = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)


def approval(
    user_id: str,
    decision: ApprovalDecision = ApprovalDecision.APPROVE,
    *roles: str,
) -> ApprovalContext:
    return ApprovalContext(
        tenant_id="tenant-a",
        approver_user_id=user_id,
        roles=roles,
        decision=decision,
        decided_at=NOW,
    )


def user_candidate(
    *,
    user_id: str = "user-a",
    value: str = "concise",
    deduplication_key: str | None = None,
    expires_at: datetime | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        owner=UserMemoryOwner(tenant_id="tenant-a", user_id=user_id),
        content=UserMemoryContent(
            preference_key="report_style",
            preference_value=value,
        ),
        source="explicit_user_instruction",
        deduplication_key=deduplication_key,
        expires_at=expires_at,
    )


def message(
    *,
    conversation_id: str,
    run_id: str,
    role: MessageRole,
    content: str,
    user_id: str = "user-a",
) -> MessageWrite:
    return MessageWrite(
        tenant_id="tenant-a",
        user_id=user_id,
        domain_id="commerce",
        conversation_id=conversation_id,
        run_id=run_id,
        role=role,
        content=content,
    )


def summary(*, conversation_id: str, run_id: str) -> ConversationSummaryWrite:
    return ConversationSummaryWrite(
        tenant_id="tenant-a",
        user_id="user-a",
        domain_id="commerce",
        conversation_id=conversation_id,
        run_id=run_id,
        summary="Conversation summary",
    )


class ApprovalStateRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = NullMemoryManager(clock=lambda: NOW)

    async def test_reject_is_authorized_before_state_transition(self) -> None:
        proposal_id = await self.manager.propose(user_candidate())

        with self.assertRaises(MemoryApprovalError):
            await self.manager.commit(
                proposal_id,
                approval("user-b", ApprovalDecision.REJECT),
            )

        self.assertEqual(
            self.manager.get_proposal(proposal_id).status,
            ProposalStatus.PENDING_APPROVAL,
        )

    async def test_only_same_authorized_terminal_decision_is_idempotent(self) -> None:
        committed = await self.manager.propose(user_candidate())
        owner_approval = approval("user-a")
        await self.manager.commit(committed, owner_approval)
        await self.manager.commit(committed, owner_approval)

        with self.assertRaises(MemoryApprovalError):
            await self.manager.commit(committed, approval("user-b"))

        rejected = await self.manager.propose(
            user_candidate(value="detailed", deduplication_key="other-slot")
        )
        owner_rejection = approval("user-a", ApprovalDecision.REJECT)
        await self.manager.commit(rejected, owner_rejection)
        await self.manager.commit(rejected, owner_rejection)

        with self.assertRaises(MemoryApprovalError):
            await self.manager.commit(rejected, approval("user-a"))

    async def test_unknown_and_cross_owner_decisions_share_the_safe_error(self) -> None:
        proposal_id = await self.manager.propose(user_candidate())

        for target, context in (
            ("proposal:unknown", approval("user-a")),
            (proposal_id, approval("user-b")),
        ):
            with self.subTest(target=target):
                with self.assertRaisesRegex(
                    MemoryApprovalError,
                    "not authorized or unavailable",
                ):
                    await self.manager.commit(target, context)


class OwnerClosureAndExpiryRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = NullMemoryManager(clock=lambda: NOW)

    def test_turn_models_expose_domain_and_owned_message_summary_contracts(self) -> None:
        self.assertIn("domain_id", ConversationWriteBatch.model_fields)
        self.assertIn("tenant_id", MessageWrite.model_fields)
        self.assertIn("domain_id", MessageWrite.model_fields)
        self.assertIn("conversation_id", MessageWrite.model_fields)
        self.assertIn("run_id", MessageWrite.model_fields)
        self.assertNotEqual(
            ConversationWriteBatch.model_fields["conversation_summary"].annotation,
            str,
        )

    def test_run_subject_scope_requires_conversation(self) -> None:
        with self.assertRaisesRegex(ValidationError, "conversation_id"):
            SubjectScope(
                tenant_id="tenant-a",
                domain_id="commerce",
                actor_user_id="user-a",
                user_id="user-a",
                run_id="run-a",
            )

    def test_batch_rejects_cross_owner_proposal_at_model_boundary(self) -> None:
        with self.assertRaisesRegex(ValidationError, "proposal owner"):
            ConversationWriteBatch(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id="conversation-a",
                run_id="run-a",
                user_message=message(
                    conversation_id="conversation-a",
                    run_id="run-a",
                    role=MessageRole.USER,
                    content="Question",
                ),
                assistant_message=message(
                    conversation_id="conversation-a",
                    run_id="run-a",
                    role=MessageRole.ASSISTANT,
                    content="Answer",
                ),
                conversation_summary=summary(
                    conversation_id="conversation-a",
                    run_id="run-a",
                ),
                proposals=(user_candidate(user_id="user-b"),),
            )

    async def test_future_as_of_does_not_permanently_expire_record(self) -> None:
        proposal_id = await self.manager.propose(
            user_candidate(expires_at=NOW + timedelta(days=1))
        )
        await self.manager.commit(proposal_id, approval("user-a"))
        base = {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "scopes": (MemoryScope.USER,),
            "query": "report",
        }

        future = await self.manager.recall(
            MemoryQuery(**base, as_of=NOW + timedelta(days=2)),
            MemoryBudget(),
        )
        earlier = await self.manager.recall(
            MemoryQuery(**base, as_of=NOW),
            MemoryBudget(),
        )

        self.assertEqual(future.records, ())
        self.assertEqual(len(earlier.records), 1)

    async def test_run_only_forget_preserves_conversations_and_other_runs(self) -> None:
        conversations = [
            await self.manager.create_conversation(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                title=title,
            )
            for title in ("First", "Second")
        ]
        proposal_ids: dict[tuple[str, str], str] = {}
        for conversation in conversations:
            for run_id in ("run-a", "run-b"):
                candidate = MemoryCandidate(
                    owner=WorkingMemoryOwner(
                        tenant_id="tenant-a",
                        user_id="user-a",
                        domain_id="commerce",
                        conversation_id=conversation.conversation_id,
                        run_id=run_id,
                    ),
                    content=WorkingMemoryContent(
                        summary=f"{conversation.title} {run_id}"
                    ),
                    source="checkpoint",
                )
                proposal_ids[(conversation.conversation_id, run_id)] = (
                    await self.manager.propose(candidate)
                )
                await self.manager.save_turn(
                    ConversationWriteBatch(
                        tenant_id="tenant-a",
                        user_id="user-a",
                        domain_id="commerce",
                        conversation_id=conversation.conversation_id,
                        run_id=run_id,
                        user_message=message(
                            conversation_id=conversation.conversation_id,
                            run_id=run_id,
                            role=MessageRole.USER,
                            content=f"question {run_id}",
                        ),
                        assistant_message=message(
                            conversation_id=conversation.conversation_id,
                            run_id=run_id,
                            role=MessageRole.ASSISTANT,
                            content=f"answer {run_id}",
                        ),
                        conversation_summary=summary(
                            conversation_id=conversation.conversation_id,
                            run_id=run_id,
                        ),
                        proposals=(candidate,),
                    )
                )

        removed = await self.manager.forget(
            SubjectScope(
                tenant_id="tenant-a",
                domain_id="commerce",
                actor_user_id="user-a",
                user_id="user-a",
                conversation_id=conversations[0].conversation_id,
                run_id="run-a",
            )
        )

        self.assertGreater(removed, 0)
        for index, conversation in enumerate(conversations):
            self.assertIsNotNone(
                await self.manager.get_conversation(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    domain_id="commerce",
                    conversation_id=conversation.conversation_id,
                )
            )
            messages = await self.manager.list_messages(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id=conversation.conversation_id,
                limit=10,
            )
            expected_runs = {"run-b"} if index == 0 else {"run-a", "run-b"}
            self.assertEqual({item.run_id for item in messages}, expected_runs)
            if index == 0:
                with self.assertRaises(MemoryProposalNotFoundError):
                    self.manager.get_proposal(
                        proposal_ids[(conversation.conversation_id, "run-a")]
                    )
            else:
                self.assertIsNotNone(
                    self.manager.get_proposal(
                        proposal_ids[(conversation.conversation_id, "run-a")]
                    )
                )
            self.assertIsNotNone(
                self.manager.get_proposal(
                    proposal_ids[(conversation.conversation_id, "run-b")]
                )
            )

    async def test_forget_is_exact_to_the_required_domain(self) -> None:
        self.assertIn("domain_id", SubjectScope.model_fields)
        self.assertTrue(SubjectScope.model_fields["domain_id"].is_required())

        conversations = {}
        for domain_id in ("commerce", "finance"):
            conversation = await self.manager.create_conversation(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id=domain_id,
                title=domain_id,
            )
            conversations[domain_id] = conversation
            owner = {
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "domain_id": domain_id,
                "conversation_id": conversation.conversation_id,
                "run_id": "run-a",
            }
            await self.manager.save_turn(
                ConversationWriteBatch(
                    **owner,
                    user_message=MessageWrite(
                        **owner,
                        role=MessageRole.USER,
                        content=f"question {domain_id}",
                    ),
                    assistant_message=MessageWrite(
                        **owner,
                        role=MessageRole.ASSISTANT,
                        content=f"answer {domain_id}",
                    ),
                    conversation_summary=ConversationSummaryWrite(
                        **owner,
                        summary=f"summary {domain_id}",
                    ),
                )
            )

        await self.manager.forget(
            SubjectScope(
                tenant_id="tenant-a",
                domain_id="commerce",
                actor_user_id="user-a",
                user_id="user-a",
            )
        )

        self.assertIsNone(
            await self.manager.get_conversation(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                conversation_id=conversations["commerce"].conversation_id,
            )
        )
        finance = await self.manager.get_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="finance",
            conversation_id=conversations["finance"].conversation_id,
        )
        self.assertIsNotNone(finance)
        self.assertEqual(finance.summary, "summary finance")

    async def test_run_forget_clears_only_summary_derived_from_that_run(self) -> None:
        self.assertIn("summary_run_id", ConversationRecord.model_fields)

        async def make_conversation(title: str, run_order: tuple[str, str]):
            conversation = await self.manager.create_conversation(
                tenant_id="tenant-a",
                user_id="user-a",
                domain_id="commerce",
                title=title,
            )
            for run_id in run_order:
                owner = {
                    "tenant_id": "tenant-a",
                    "user_id": "user-a",
                    "domain_id": "commerce",
                    "conversation_id": conversation.conversation_id,
                    "run_id": run_id,
                }
                await self.manager.save_turn(
                    ConversationWriteBatch(
                        **owner,
                        user_message=MessageWrite(
                            **owner,
                            role=MessageRole.USER,
                            content=f"question {run_id}",
                        ),
                        assistant_message=MessageWrite(
                            **owner,
                            role=MessageRole.ASSISTANT,
                            content=f"answer {run_id}",
                        ),
                        conversation_summary=ConversationSummaryWrite(
                            **owner,
                            summary=f"summary {run_id}",
                        ),
                    )
                )
            return conversation

        target_latest = await make_conversation("target latest", ("run-b", "run-a"))
        other_latest = await make_conversation("other latest", ("run-a", "run-b"))
        for conversation in (target_latest, other_latest):
            await self.manager.forget(
                SubjectScope(
                    tenant_id="tenant-a",
                    domain_id="commerce",
                    actor_user_id="user-a",
                    user_id="user-a",
                    conversation_id=conversation.conversation_id,
                    run_id="run-a",
                )
            )

        cleared = await self.manager.get_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            conversation_id=target_latest.conversation_id,
        )
        retained = await self.manager.get_conversation(
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            conversation_id=other_latest.conversation_id,
        )
        self.assertEqual((cleared.summary, cleared.summary_run_id), ("", None))
        self.assertEqual(
            (retained.summary, retained.summary_run_id),
            ("summary run-b", "run-b"),
        )

    async def test_custom_deduplication_key_is_isolated_by_full_owner(self) -> None:
        first = await self.manager.propose(
            user_candidate(user_id="user-a", deduplication_key="shared-key")
        )
        second = await self.manager.propose(
            user_candidate(user_id="user-b", deduplication_key="shared-key")
        )

        self.assertNotEqual(first, second)
        self.assertEqual(
            self.manager.get_proposal(first).status,
            ProposalStatus.PENDING_APPROVAL,
        )
        self.assertEqual(
            self.manager.get_proposal(second).status,
            ProposalStatus.PENDING_APPROVAL,
        )

    async def test_pending_same_owner_slot_cannot_double_commit(self) -> None:
        first = await self.manager.propose(
            user_candidate(value="concise", deduplication_key="same-slot")
        )
        second = await self.manager.propose(
            user_candidate(value="detailed", deduplication_key="same-slot")
        )

        outcomes = await asyncio.gather(
            self.manager.commit(first, approval("user-a")),
            self.manager.commit(second, approval("user-a")),
            return_exceptions=True,
        )

        self.assertEqual(len(self.manager.records), 1)
        self.assertEqual(sum(item is None for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, MemoryConflictError) for item in outcomes), 1)
        self.assertEqual(
            {
                self.manager.get_proposal(first).status,
                self.manager.get_proposal(second).status,
            },
            {ProposalStatus.COMMITTED, ProposalStatus.CONFLICT},
        )


if __name__ == "__main__":
    unittest.main()
