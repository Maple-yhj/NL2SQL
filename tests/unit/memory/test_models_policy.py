from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from data_agent.memory.models import (
    ApprovalContext,
    ApprovalDecision,
    ArtifactReference,
    EnterpriseMemoryContent,
    EnterpriseMemoryOwner,
    MemoryBudget,
    MemoryCandidate,
    MemoryScope,
    UserMemoryContent,
    UserMemoryOwner,
)
from data_agent.memory.policy import MemoryPolicyError, validate_candidate_content
import data_agent.memory as memory_api


class MemoryModelTests(unittest.TestCase):
    def test_public_model_exports_do_not_leak_dependency_symbols(self) -> None:
        for leaked in ("BaseModel", "ConfigDict", "ExecutionCheckpoint", "datetime"):
            self.assertFalse(hasattr(memory_api, leaked), leaked)

    def test_scope_specific_owner_and_content_are_exact_and_frozen(self) -> None:
        candidate = MemoryCandidate(
            owner=UserMemoryOwner(tenant_id="tenant-a", user_id="user-a"),
            content=UserMemoryContent(
                preference_key="report_style",
                preference_value="weekly concise summary",
            ),
            source="explicit_user_instruction",
        )

        self.assertEqual(candidate.scope, MemoryScope.USER)
        with self.assertRaises(ValidationError):
            candidate.model_copy(update={"content": {"kind": "anything"}})
        with self.assertRaises(ValidationError):
            UserMemoryOwner(
                tenant_id="tenant-a",
                user_id="user-a",
                conversation_id="conversation-a",
            )

    def test_owner_and_content_scopes_must_match(self) -> None:
        with self.assertRaisesRegex(ValidationError, "owner and content scopes"):
            MemoryCandidate(
                owner=EnterpriseMemoryOwner(
                    tenant_id="tenant-a",
                    domain_id="commerce",
                ),
                content=UserMemoryContent(
                    preference_key="currency",
                    preference_value="BRL",
                ),
                source="explicit_user_instruction",
            )

    def test_enterprise_approval_requires_admin_and_user_requires_subject(self) -> None:
        enterprise = EnterpriseMemoryContent(
            category="business_term",
            statement="GMV excludes cancelled orders.",
        )
        owner = EnterpriseMemoryOwner(
            tenant_id="tenant-a",
            domain_id="commerce",
        )
        non_admin = ApprovalContext(
            tenant_id="tenant-a",
            approver_user_id="user-a",
            roles=("analyst",),
            decision=ApprovalDecision.APPROVE,
            decided_at=datetime(2026, 7, 11, tzinfo=UTC),
        )
        user_admin = non_admin.model_copy(
            update={"approver_user_id": "admin-a", "roles": ("admin",)}
        )

        self.assertFalse(non_admin.authorizes(owner, enterprise))
        self.assertTrue(user_admin.authorizes(owner, enterprise))

        user_owner = UserMemoryOwner(tenant_id="tenant-a", user_id="user-a")
        user_content = UserMemoryContent(
            preference_key="chart",
            preference_value="bar",
        )
        self.assertTrue(non_admin.authorizes(user_owner, user_content))
        stranger = non_admin.model_copy(update={"approver_user_id": "user-b"})
        self.assertFalse(stranger.authorizes(user_owner, user_content))
        self.assertTrue(user_admin.authorizes(user_owner, user_content))

    def test_budget_enforces_record_token_and_character_caps(self) -> None:
        with self.assertRaises(ValidationError):
            MemoryBudget(max_records=0)
        with self.assertRaises(ValidationError):
            MemoryBudget(max_records=1, max_tokens=16, max_characters=8)


class MemoryContentPolicyTests(unittest.TestCase):
    def test_recursive_policy_rejects_secrets_connections_raw_params_and_results(self) -> None:
        forbidden = (
            {"nested": {"password": "open-sesame"}},
            {"connection": "postgresql://reader:secret@db/olist"},
            {"sql_params": {"customer_email": "person@example.com"}},
            {"rows": [{"order_id": "1"}, {"order_id": "2"}]},
            {"domain_pack": {"metrics": ["gmv"]}},
            {"policy": {"allowed_relations": ["orders"]}},
        )

        for value in forbidden:
            with self.subTest(value=value):
                with self.assertRaises(MemoryPolicyError):
                    validate_candidate_content(value)

    def test_policy_accepts_typed_safe_summary_and_artifact_reference(self) -> None:
        reference = ArtifactReference(
            artifact_id="query-result:abc123",
            tenant_id="tenant-a",
            user_id="user-a",
            domain_id="commerce",
            conversation_id="conversation-a",
            run_id="run-a",
            kind="query_result",
            digest="a" * 64,
            row_count=500,
        )
        value = {
            "summary": "Orders increased week over week.",
            "row_count": 500,
            "artifact_refs": [reference.model_dump(mode="json")],
        }

        validate_candidate_content(value)


if __name__ == "__main__":
    unittest.main()
