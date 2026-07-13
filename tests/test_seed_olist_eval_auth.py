from __future__ import annotations

import os
import unittest
from unittest import mock

from api.auth import verify_password
from scripts.seed_olist_eval_auth import (
    OLIST_EVAL_ADMIN_TENANT_ID,
    OLIST_EVAL_SELLER_TENANT_ID,
    build_olist_eval_user_payloads,
    seed_olist_eval_users,
)


class RecordingAuthStore:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def upsert_user(self, **payload):
        self.payloads.append(payload)
        return payload


class OlistEvalAuthSeedTests(unittest.IsolatedAsyncioTestCase):
    def test_payloads_cover_admin_and_seller_without_a_default_password(self):
        payloads = build_olist_eval_user_payloads(
            username="eval-user",
            password="test-secret",
        )

        self.assertEqual(
            {payload["tenant_id"] for payload in payloads},
            {OLIST_EVAL_ADMIN_TENANT_ID, OLIST_EVAL_SELLER_TENANT_ID},
        )
        self.assertTrue(
            all(verify_password("test-secret", payload["password_hash"]) for payload in payloads)
        )

    def test_missing_password_fails_closed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "password"):
                build_olist_eval_user_payloads(username="eval-user")

    async def test_seed_upserts_both_tenants_with_one_store(self):
        store = RecordingAuthStore()

        with mock.patch(
            "scripts.seed_olist_eval_auth.create_auth_store",
            return_value=store,
        ):
            users = await seed_olist_eval_users(
                username="eval-user",
                password="test-secret",
            )

        self.assertEqual(len(users), 2)
        self.assertEqual(len(store.payloads), 2)
        self.assertTrue(all(payload["disabled"] is False for payload in store.payloads))


if __name__ == "__main__":
    unittest.main()
