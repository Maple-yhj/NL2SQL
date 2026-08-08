from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from data_agent.analysis_agent.artifacts import (
    ArtifactStoreError,
    SQLiteArtifactStore,
)
from data_agent.runtime.errors import ErrorCode


class ArtifactStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.now = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)
        self.store = SQLiteArtifactStore(self.root, clock=lambda: self.now)

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def put(self, **overrides: object):
        values: dict[str, object] = {
            "tenant_id": "tenant-1",
            "user_id": "user-1",
            "run_id": "run-1",
            "call_id": "call-1",
            "kind": "query_result",
            "payload": {"rows": [{"city": "Shanghai", "revenue": 42}]},
            "schema_digest": "a" * 64,
            "row_count": 1,
            "sensitivity": "derived",
            "retention_seconds": 3600,
        }
        values.update(overrides)
        return await self.store.put_json(**values)

    async def test_json_round_trip_digest_and_idempotent_call(self) -> None:
        first = await self.put()
        second = await self.put()

        self.assertEqual(first, second)
        self.assertEqual(first.run_id, "run-1")
        self.assertEqual(len(first.digest), 64)
        self.assertEqual(
            await self.store.get_json(
                tenant_id="tenant-1",
                user_id="user-1",
                run_id="run-1",
                artifact_id=first.artifact_id,
            ),
            {"rows": [{"city": "Shanghai", "revenue": 42}]},
        )
        self.assertEqual(
            await self.store.list_for_run(
                tenant_id="tenant-1",
                user_id="user-1",
                run_id="run-1",
            ),
            (first,),
        )

    async def test_same_call_with_different_payload_is_a_conflict(self) -> None:
        await self.put()
        with self.assertRaisesRegex(ArtifactStoreError, "different payload"):
            await self.put(payload={"rows": [{"revenue": 99}]})

    async def test_reads_are_isolated_by_tenant_user_and_run(self) -> None:
        artifact = await self.put()
        for owner in (
            {"tenant_id": "other", "user_id": "user-1", "run_id": "run-1"},
            {"tenant_id": "tenant-1", "user_id": "other", "run_id": "run-1"},
            {"tenant_id": "tenant-1", "user_id": "user-1", "run_id": "other"},
        ):
            with self.assertRaises(ArtifactStoreError) as caught:
                await self.store.get_json(
                    **owner,
                    artifact_id=artifact.artifact_id,
                )
            self.assertEqual(caught.exception.code, ErrorCode.AGENT_ARTIFACT_NOT_FOUND)

    async def test_user_controlled_paths_and_extensions_are_rejected(self) -> None:
        for call_id in ("../escape", "/absolute", "call.json", "call/child"):
            with self.assertRaises(ValueError):
                await self.put(call_id=call_id)
        with self.assertRaises(ArtifactStoreError):
            await self.store.get_json(
                tenant_id="tenant-1",
                user_id="user-1",
                run_id="run-1",
                artifact_id="../secret.json",
            )

    async def test_payload_tampering_is_detected(self) -> None:
        artifact = await self.put()
        payload_path = next(self.store.payload_root.rglob("*.json"))
        payload_path.write_text('{"tampered":true}', encoding="utf-8")

        with self.assertRaises(ArtifactStoreError) as caught:
            await self.store.get_json(
                tenant_id="tenant-1",
                user_id="user-1",
                run_id="run-1",
                artifact_id=artifact.artifact_id,
            )
        self.assertEqual(
            caught.exception.code,
            ErrorCode.AGENT_ARTIFACT_INTEGRITY_ERROR,
        )

    async def test_atomic_replace_failure_leaves_no_visible_artifact(self) -> None:
        def fail_replace(source: str | Path, destination: str | Path) -> None:
            del source, destination
            raise OSError("simulated replace failure")

        store = SQLiteArtifactStore(
            self.root / "failing",
            clock=lambda: self.now,
            atomic_replace=fail_replace,
        )
        with self.assertRaises(OSError):
            await store.put_json(
                tenant_id="tenant-1",
                user_id="user-1",
                run_id="run-1",
                call_id="call-1",
                kind="catalog",
                payload={"relations": []},
                sensitivity="metadata",
            )
        self.assertEqual(
            await store.list_for_run(
                tenant_id="tenant-1",
                user_id="user-1",
                run_id="run-1",
            ),
            (),
        )
        self.assertEqual(tuple(store.payload_root.rglob("*.json")), ())
        self.assertEqual(tuple(store.payload_root.rglob("*.tmp")), ())

    async def test_safe_preview_is_bounded_and_redacts_sensitive_columns(self) -> None:
        artifact = await self.put(
            payload={
                "rows": [
                    {
                        "customer_email": "a@example.com",
                        "revenue": 42,
                        "description": "x" * 100,
                        "ignored": "too many columns",
                    },
                    {"customer_email": "b@example.com", "revenue": 11},
                ]
            },
            row_count=2,
        )
        preview = await self.store.get_safe_preview(
            tenant_id="tenant-1",
            user_id="user-1",
            run_id="run-1",
            artifact_id=artifact.artifact_id,
            max_rows=1,
            max_columns=3,
            max_string_chars=12,
            max_depth=3,
        )

        self.assertEqual(len(preview["rows"]), 1)
        self.assertEqual(preview["rows"][0]["customer_email"], "[REDACTED]")
        self.assertLessEqual(len(preview["rows"][0]), 3)
        self.assertEqual(preview["rows"][0]["description"], "xxxxxxxxxxxx…")

    async def test_retention_deletes_only_expired_unretained_artifacts(self) -> None:
        expired = await self.put(call_id="expired", retention_seconds=1)
        retained = await self.put(
            call_id="retained",
            retention_seconds=1,
            retained=True,
        )
        current = await self.put(call_id="current", retention_seconds=3600)
        self.now += timedelta(seconds=2)

        deleted = await self.store.delete_expired(now=self.now)

        self.assertEqual(deleted, (expired.artifact_id,))
        remaining = await self.store.list_for_run(
            tenant_id="tenant-1",
            user_id="user-1",
            run_id="run-1",
        )
        self.assertEqual(
            {item.artifact_id for item in remaining},
            {retained.artifact_id, current.artifact_id},
        )
        self.assertEqual(len(tuple(self.store.payload_root.rglob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
