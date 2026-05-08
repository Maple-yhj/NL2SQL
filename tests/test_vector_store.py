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


if __name__ == "__main__":
    unittest.main()
