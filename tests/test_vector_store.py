import asyncio
import os
import sys
import types
import unittest
from unittest import mock

from rag import vector_store


class ConnectVectorStoreTests(unittest.TestCase):
    def test_connect_vector_store_uses_postgres_dsn_and_registers_vector_codec(self):
        calls = []
        conn = object()

        async def fake_connect(dsn):
            calls.append(("connect", dsn))
            return conn

        async def fake_register_vector(connection):
            calls.append(("register", connection))

        fake_asyncpg = types.SimpleNamespace(connect=fake_connect)
        fake_pgvector_asyncpg = types.SimpleNamespace(register_vector=fake_register_vector)

        with mock.patch.dict(
            sys.modules,
            {
                "asyncpg": fake_asyncpg,
                "pgvector": types.SimpleNamespace(asyncpg=fake_pgvector_asyncpg),
                "pgvector.asyncpg": fake_pgvector_asyncpg,
            },
        ), mock.patch.dict(
            os.environ,
            {"POSTGRES_DSN": "postgresql://example/db"},
            clear=True,
        ), mock.patch.object(vector_store, "load_env_file") as load_env_file:
            result = asyncio.run(vector_store.connect_vector_store())

        self.assertIs(result, conn)
        load_env_file.assert_called_once_with()
        self.assertEqual(
            calls,
            [
                ("connect", "postgresql://example/db"),
                ("register", conn),
            ],
        )

    def test_search_semantic_index_awaits_connection_close(self):
        calls = []

        class FakeConnection:
            async def fetch(self, *args):
                calls.append(("fetch", args[1], args[2], args[3], args[4]))
                return []

            async def close(self):
                calls.append(("close",))

        async def fake_connect_vector_store(dsn=None):
            calls.append(("connect", dsn))
            return FakeConnection()

        with mock.patch.object(
            vector_store,
            "connect_vector_store",
            side_effect=fake_connect_vector_store,
        ):
            result = asyncio.run(
                vector_store.search_semantic_index(
                    [0.0] * vector_store.DEFAULT_EMBEDDING_DIM,
                    tenant_id="demo",
                    object_types=["metric"],
                    top_k=3,
                    dsn="postgresql://example/db",
                )
            )

        self.assertEqual(result, [])
        self.assertEqual(calls[0], ("connect", "postgresql://example/db"))
        self.assertEqual(calls[-1], ("close",))


if __name__ == "__main__":
    unittest.main()
