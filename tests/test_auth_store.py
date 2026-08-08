from __future__ import annotations

import asyncio
import os
from functools import wraps
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from api.auth_store import InMemoryAuthStore, PostgresAuthStore, create_auth_store, hash_refresh_token


def async_test(test_method):
    @wraps(test_method)
    def wrapper(self):
        return self.run_async(test_method(self))

    return wrapper


class InMemoryAuthStoreTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def setUp(self):
        self.store = InMemoryAuthStore()
        self.run_async(
            self.store.upsert_user(
                tenant_id="tenant-1",
                user_id="user-1",
                username="alice",
                password_hash="hash-1",
                roles=["admin", "analyst"],
            )
        )

    @async_test
    async def test_find_user_by_login_returns_active_and_disabled_users(self):
        await self.store.upsert_user(
            tenant_id="tenant-1",
            user_id="user-2",
            username="bob",
            password_hash="hash-2",
            roles=["user"],
            disabled=True,
        )

        active = await self.store.find_user_by_login("tenant-1", "alice")
        disabled = await self.store.find_user_by_login("tenant-1", "bob")

        self.assertIsNotNone(active)
        self.assertFalse(active["disabled"])
        self.assertEqual(disabled["user_id"], "user-2")
        self.assertTrue(disabled["disabled"])

    @async_test
    async def test_get_user_returns_user_by_tenant_and_user(self):
        user = await self.store.get_user("tenant-1", "user-1")

        self.assertEqual(user["username"], "alice")
        self.assertIsNone(await self.store.get_user("tenant-2", "user-1"))

    @async_test
    async def test_upsert_user_updates_fields_and_preserves_token_version(self):
        before = await self.store.get_user("tenant-1", "user-1")

        await self.store.upsert_user(
            tenant_id="tenant-1",
            user_id="user-1",
            username="alice-renamed",
            password_hash="hash-2",
            roles=["user"],
            disabled=True,
        )
        after = await self.store.get_user("tenant-1", "user-1")

        self.assertEqual(after["username"], "alice-renamed")
        self.assertEqual(after["password_hash"], "hash-2")
        self.assertEqual(after["roles"], ["user"])
        self.assertTrue(after["disabled"])
        self.assertEqual(after["token_version"], before["token_version"])
        self.assertIsNone(await self.store.find_user_by_login("tenant-1", "alice"))
        self.assertEqual(
            (await self.store.find_user_by_login("tenant-1", "alice-renamed"))["user_id"],
            "user-1",
        )

    @async_test
    async def test_upsert_user_rejects_duplicate_username_for_different_user(self):
        with self.assertRaises(ValueError):
            await self.store.upsert_user(
                tenant_id="tenant-1",
                user_id="user-2",
                username="alice",
                password_hash="hash-2",
                roles=["user"],
            )

        user = await self.store.get_user("tenant-1", "user-1")
        self.assertEqual(user["username"], "alice")
        self.assertIsNone(await self.store.get_user("tenant-1", "user-2"))

    @async_test
    async def test_store_and_get_active_refresh_token(self):
        expires_at = datetime.now(UTC) + timedelta(hours=1)

        await self.store.store_refresh_token(
            token_id="token-1",
            token_hash="hash-1",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=expires_at,
            user_agent="pytest",
            client_ip="127.0.0.1",
        )

        record = await self.store.get_active_refresh_token("token-1", "hash-1")

        self.assertEqual(record["tenant_id"], "tenant-1")
        self.assertEqual(record["user_id"], "user-1")
        self.assertEqual(record["expires_at"], expires_at)
        self.assertEqual(record["user_agent"], "pytest")
        self.assertEqual(record["client_ip"], "127.0.0.1")
        self.assertIsNone(record["revoked_at"])

    @async_test
    async def test_store_refresh_token_rejects_duplicate_token_id(self):
        await self.store.store_refresh_token(
            token_id="token-1",
            token_hash="hash-1",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        with self.assertRaises(ValueError):
            await self.store.store_refresh_token(
                token_id="token-1",
                token_hash="hash-2",
                tenant_id="tenant-1",
                user_id="user-1",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

        self.assertIsNone(await self.store.get_active_refresh_token("token-1", "hash-2"))

    @async_test
    async def test_store_refresh_token_rejects_duplicate_token_hash(self):
        await self.store.store_refresh_token(
            token_id="token-1",
            token_hash="hash-1",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        with self.assertRaises(ValueError):
            await self.store.store_refresh_token(
                token_id="token-2",
                token_hash="hash-1",
                tenant_id="tenant-1",
                user_id="user-1",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

        self.assertIsNone(await self.store.get_active_refresh_token("token-2", "hash-1"))

    @async_test
    async def test_expired_refresh_token_is_inactive(self):
        await self.store.store_refresh_token(
            token_id="expired",
            token_hash="hash-expired",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )

        self.assertIsNone(
            await self.store.get_active_refresh_token("expired", "hash-expired")
        )

    @async_test
    async def test_token_hash_mismatch_is_inactive(self):
        await self.store.store_refresh_token(
            token_id="token-1",
            token_hash="hash-1",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        self.assertIsNone(await self.store.get_active_refresh_token("token-1", "wrong"))

    @async_test
    async def test_rotation_revokes_old_token_and_stores_new_token(self):
        await self.store.store_refresh_token(
            token_id="old-token",
            token_hash="old-hash",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        await self.store.rotate_refresh_token(
            old_token_id="old-token",
            old_token_hash="old-hash",
            new_token_id="new-token",
            new_token_hash="new-hash",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            user_agent="pytest",
            client_ip="127.0.0.1",
        )

        self.assertIsNone(await self.store.get_active_refresh_token("old-token", "old-hash"))
        old_record = self.store._refresh_tokens["old-token"]
        self.assertIsNotNone(old_record["revoked_at"])
        self.assertEqual(old_record["replaced_by_token_id"], "new-token")

        new_record = await self.store.get_active_refresh_token("new-token", "new-hash")
        self.assertEqual(new_record["user_id"], "user-1")
        self.assertEqual(new_record["user_agent"], "pytest")

    @async_test
    async def test_rotation_missing_old_token_raises_and_does_not_store_new_token(self):
        with self.assertRaises(ValueError):
            await self.store.rotate_refresh_token(
                old_token_id="missing-token",
                old_token_hash="missing-hash",
                new_token_id="new-token",
                new_token_hash="new-hash",
                tenant_id="tenant-1",
                user_id="user-1",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

        self.assertIsNone(await self.store.get_active_refresh_token("new-token", "new-hash"))

    @async_test
    async def test_rotation_revoked_old_token_raises_and_does_not_store_new_token(self):
        await self.store.store_refresh_token(
            token_id="old-token",
            token_hash="old-hash",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await self.store.revoke_refresh_token("old-token", "old-hash")

        with self.assertRaises(ValueError):
            await self.store.rotate_refresh_token(
                old_token_id="old-token",
                old_token_hash="old-hash",
                new_token_id="new-token",
                new_token_hash="new-hash",
                tenant_id="tenant-1",
                user_id="user-1",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

        self.assertIsNone(await self.store.get_active_refresh_token("new-token", "new-hash"))

    @async_test
    async def test_rotation_expired_old_token_raises_and_does_not_store_new_token(self):
        await self.store.store_refresh_token(
            token_id="old-token",
            token_hash="old-hash",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )

        with self.assertRaises(ValueError):
            await self.store.rotate_refresh_token(
                old_token_id="old-token",
                old_token_hash="old-hash",
                new_token_id="new-token",
                new_token_hash="new-hash",
                tenant_id="tenant-1",
                user_id="user-1",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

        self.assertIsNone(await self.store.get_active_refresh_token("new-token", "new-hash"))

    @async_test
    async def test_rotation_tenant_user_mismatch_raises_and_does_not_store_new_token(self):
        await self.store.store_refresh_token(
            token_id="old-token",
            token_hash="old-hash",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        with self.assertRaises(ValueError):
            await self.store.rotate_refresh_token(
                old_token_id="old-token",
                old_token_hash="old-hash",
                new_token_id="new-token",
                new_token_hash="new-hash",
                tenant_id="tenant-1",
                user_id="other-user",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

        old_record = await self.store.get_active_refresh_token("old-token", "old-hash")
        self.assertIsNotNone(old_record)
        self.assertIsNone(await self.store.get_active_refresh_token("new-token", "new-hash"))

    @async_test
    async def test_rotation_old_token_hash_mismatch_raises_and_does_not_store_new_token(self):
        await self.store.store_refresh_token(
            token_id="old-token",
            token_hash="old-hash",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        with self.assertRaises(ValueError):
            await self.store.rotate_refresh_token(
                old_token_id="old-token",
                old_token_hash="wrong-hash",
                new_token_id="new-token",
                new_token_hash="new-hash",
                tenant_id="tenant-1",
                user_id="user-1",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

        old_record = await self.store.get_active_refresh_token("old-token", "old-hash")
        self.assertIsNotNone(old_record)
        self.assertIsNone(await self.store.get_active_refresh_token("new-token", "new-hash"))

    @async_test
    async def test_revoke_refresh_token_makes_token_inactive(self):
        await self.store.store_refresh_token(
            token_id="token-1",
            token_hash="hash-1",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        await self.store.revoke_refresh_token("token-1", "hash-1")

        self.assertIsNone(await self.store.get_active_refresh_token("token-1", "hash-1"))

    @async_test
    async def test_returned_user_and_refresh_records_are_copies(self):
        await self.store.store_refresh_token(
            token_id="token-1",
            token_hash="hash-1",
            tenant_id="tenant-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        user = await self.store.get_user("tenant-1", "user-1")
        user["roles"].append("mutated")
        user["username"] = "mutated"

        token = await self.store.get_active_refresh_token("token-1", "hash-1")
        token["tenant_id"] = "mutated"

        unchanged_user = await self.store.get_user("tenant-1", "user-1")
        unchanged_token = await self.store.get_active_refresh_token("token-1", "hash-1")

        self.assertEqual(unchanged_user["username"], "alice")
        self.assertEqual(unchanged_user["roles"], ["admin", "analyst"])
        self.assertEqual(unchanged_token["tenant_id"], "tenant-1")


class RefreshTokenHashTests(unittest.TestCase):
    def test_hash_refresh_token_is_deterministic_and_not_raw_token(self):
        first = hash_refresh_token("raw-refresh-token")
        second = hash_refresh_token("raw-refresh-token")

        self.assertEqual(first, second)
        self.assertNotEqual(first, "raw-refresh-token")
        self.assertEqual(len(first), 64)


class CreateAuthStoreTests(unittest.TestCase):
    def test_create_auth_store_loads_project_environment_before_reading_env_dsn(self):
        for key in ("AUTH_DATABASE_URL", "DATABASE_URL"):
            os.environ.pop(key, None)

        with mock.patch.dict(
            os.environ,
            {"AUTH_DATABASE_URL": "postgresql://auth"},
            clear=False,
        ), mock.patch("api.auth_store.load_project_environment") as load_environment:
            store = create_auth_store()

        self.assertIsInstance(store, PostgresAuthStore)
        self.assertEqual(store.dsn, "postgresql://auth")
        load_environment.assert_called_once_with()

    def test_create_auth_store_defaults_to_the_governed_database_url(self):
        with mock.patch.dict(
            os.environ,
            {"DATABASE_URL": "postgresql://data-agent"},
            clear=True,
        ), mock.patch("api.auth_store.load_project_environment") as load_environment:
            store = create_auth_store()

        self.assertIsInstance(store, PostgresAuthStore)
        self.assertEqual(store.dsn, "postgresql://data-agent")
        load_environment.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
