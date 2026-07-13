from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(SRC_ROOT))


class ContextAssemblerTests(unittest.TestCase):
    def setUp(self) -> None:
        from data_agent.runtime.context import (
            ContextAssembler,
            ContextBudget,
            ContextBudgetExceededError,
            ContextItem,
            ContextOwner,
            ContextSource,
            ContextVersionPins,
            SecurityContext,
        )
        from data_agent.runtime.models import PrincipalContext

        self.ContextAssembler = ContextAssembler
        self.ContextBudget = ContextBudget
        self.ContextBudgetExceededError = ContextBudgetExceededError
        self.ContextItem = ContextItem
        self.ContextOwner = ContextOwner
        self.ContextSource = ContextSource
        self.ContextVersionPins = ContextVersionPins
        self.SecurityContext = SecurityContext
        self.principal = PrincipalContext(
            tenant_id="tenant-1",
            user_id="user-1",
            roles=("analyst",),
        )
        self.now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
        self.pins = ContextVersionPins(
            domain_version="1.0.0",
            binding_version="1.0.0",
            skill_version="1.0.0",
            schema_fingerprint="schema-v1",
        )

    def item(self, *, source, key, content, **overrides):
        values = {
            "source": source,
            "key": key,
            "content": content,
            "version": "1.0.0",
            "trust_level": "verified",
            "sensitivity": "internal",
            "token_cost": 2,
            "valid_from": self.now - timedelta(minutes=1),
            "expires_at": self.now + timedelta(hours=1),
            "relevance": 1.0,
        }
        values.update(overrides)
        return self.ContextItem(**values)

    def security(self):
        return self.SecurityContext(
            principal=self.principal,
            rules=(
                self.item(
                    source=self.ContextSource.SECURITY,
                    key="tenant_scope",
                    content="tenant_id = tenant-1",
                    sensitivity="restricted",
                ),
            ),
        )

    def assemble(self, items, *, budget=None):
        return self.ContextAssembler().assemble(
            security_context=self.security(),
            items=tuple(items),
            pins=self.pins,
            budget=budget or self.ContextBudget(max_items=16, max_tokens=64),
            now=self.now,
            domain_id="commerce",
            conversation_id="conversation-1",
            run_id="run-1",
        )

    def test_precedence_is_fixed_and_lower_priority_cannot_override(self) -> None:
        items = (
            self.item(
                source=self.ContextSource.CONVERSATION,
                key="metric.gmv",
                content="conversation override",
                owner=self.ContextOwner(
                    tenant_id="tenant-1",
                    user_id="user-1",
                    domain_id="commerce",
                    conversation_id="conversation-1",
                ),
                approved=True,
            ),
            self.item(
                source=self.ContextSource.DOMAIN,
                key="metric.gmv",
                content="canonical GMV definition",
            ),
            self.item(
                source=self.ContextSource.SKILL,
                key="bounded_detail",
                content="limit is required",
            ),
        )

        envelope = self.assemble(reversed(items))

        self.assertEqual(
            [item.source for item in envelope.items],
            [
                self.ContextSource.SECURITY,
                self.ContextSource.DOMAIN,
                self.ContextSource.SKILL,
            ],
        )
        by_key = {item.key: item.content for item in envelope.items}
        self.assertEqual(by_key["metric.gmv"], "canonical GMV definition")
        self.assertNotIn("conversation override", by_key.values())

    def test_owner_approval_expiry_and_version_filters_fail_closed(self) -> None:
        owner = self.ContextOwner(
            tenant_id="tenant-1",
            user_id="user-1",
            domain_id="commerce",
            conversation_id="conversation-1",
        )
        items = (
            self.item(
                source=self.ContextSource.APPROVED_ENTERPRISE_MEMORY,
                key="enterprise.pending",
                content="not approved",
                owner=self.ContextOwner(tenant_id="tenant-1", domain_id="commerce"),
                approved=False,
            ),
            self.item(
                source=self.ContextSource.USER_MEMORY,
                key="user.other",
                content="wrong user",
                owner=self.ContextOwner(tenant_id="tenant-1", user_id="user-2"),
                approved=True,
            ),
            self.item(
                source=self.ContextSource.CONVERSATION,
                key="conversation.expired",
                content="expired",
                owner=owner,
                approved=True,
                expires_at=self.now,
            ),
            self.item(
                source=self.ContextSource.CONVERSATION,
                key="conversation.stale",
                content="stale",
                owner=owner,
                approved=True,
                version_pins=self.ContextVersionPins(domain_version="0.9.0"),
            ),
            self.item(
                source=self.ContextSource.CONVERSATION,
                key="conversation.valid",
                content="approved and owned",
                owner=owner,
                approved=True,
                version_pins=self.ContextVersionPins(domain_version="1.0.0"),
            ),
        )

        envelope = self.assemble(items)

        self.assertEqual(
            [item.key for item in envelope.conversation_context],
            ["conversation.valid"],
        )

    def test_budget_selection_is_deterministic_and_security_is_mandatory(self) -> None:
        items = (
            self.item(
                source=self.ContextSource.DOMAIN,
                key="domain.a",
                content="a",
                relevance=0.9,
                token_cost=2,
            ),
            self.item(
                source=self.ContextSource.APPROVED_ENTERPRISE_MEMORY,
                key="enterprise.a",
                content="enterprise",
                relevance=1.0,
                token_cost=2,
                owner=self.ContextOwner(tenant_id="tenant-1", domain_id="commerce"),
                approved=True,
            ),
            self.item(
                source=self.ContextSource.CONVERSATION,
                key="conversation.a",
                content="conversation",
                relevance=1.0,
                token_cost=2,
                owner=self.ContextOwner(
                    tenant_id="tenant-1",
                    user_id="user-1",
                    domain_id="commerce",
                    conversation_id="conversation-1",
                ),
                approved=True,
            ),
        )
        budget = self.ContextBudget(max_items=3, max_tokens=6)

        first = self.assemble(items, budget=budget)
        second = self.assemble(reversed(items), budget=budget)

        self.assertEqual(first, second)
        self.assertEqual(
            [item.key for item in first.items],
            ["tenant_scope", "domain.a", "enterprise.a"],
        )
        self.assertEqual(first.used_tokens, 6)
        self.assertTrue(first.truncated)

    def test_oversized_higher_memory_claim_reserves_key_from_lower_layer(self) -> None:
        shared_key = "memory.shared_rule"
        items = (
            self.item(
                source=self.ContextSource.APPROVED_ENTERPRISE_MEMORY,
                key=shared_key,
                content="enterprise authority",
                owner=self.ContextOwner(tenant_id="tenant-1", domain_id="commerce"),
                approved=True,
                token_cost=8,
            ),
            self.item(
                source=self.ContextSource.CONVERSATION,
                key=shared_key,
                content="small conversation override",
                owner=self.ContextOwner(
                    tenant_id="tenant-1",
                    user_id="user-1",
                    domain_id="commerce",
                    conversation_id="conversation-1",
                ),
                approved=True,
                token_cost=1,
            ),
        )

        envelope = self.assemble(
            items,
            budget=self.ContextBudget(max_items=2, max_tokens=3),
        )

        self.assertNotIn(shared_key, {item.key for item in envelope.items})
        self.assertTrue(envelope.truncated)

    def test_oversized_mandatory_claim_fails_instead_of_selecting_small_override(self) -> None:
        shared_key = "metric.gmv"
        items = (
            self.item(
                source=self.ContextSource.DOMAIN,
                key=shared_key,
                content="canonical hard rule",
                token_cost=8,
            ),
            self.item(
                source=self.ContextSource.CONVERSATION,
                key=shared_key,
                content="small override",
                owner=self.ContextOwner(
                    tenant_id="tenant-1",
                    user_id="user-1",
                    domain_id="commerce",
                    conversation_id="conversation-1",
                ),
                approved=True,
                token_cost=1,
            ),
        )

        with self.assertRaises(self.ContextBudgetExceededError) as raised:
            self.assemble(
                items,
                budget=self.ContextBudget(max_items=2, max_tokens=3),
            )

        self.assertEqual(raised.exception.code, "CONTEXT_BUDGET_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
