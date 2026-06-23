import builtins
import importlib
import os
import types
import unittest
from unittest import mock

from graph.tools.explain_result import explain_result
from graph.tools.validate_sql import validate_sql


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def complete(self, prompt, system="", max_output_tokens=2048):
        self.calls.append({"prompt": prompt, "system": system})
        return "GMV is 100."


class GraphToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_enforces_authorized_tables_and_limit(self):
        valid = await validate_sql(
            "SELECT amount FROM orders",
            tenant_id="demo",
            allowed_tables=["orders"],
            max_limit=100,
        )
        invalid = await validate_sql(
            "SELECT * FROM users",
            tenant_id="demo",
            allowed_tables=["orders"],
            max_limit=100,
        )

        self.assertTrue(valid["ok"])
        self.assertIn("LIMIT 100", valid["normalized_sql"])
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["violations"][0]["code"], "table_not_allowed")

    async def test_validation_rejects_missing_authorized_table_scope(self):
        result = await validate_sql(
            "SELECT amount FROM orders",
            tenant_id="demo",
            allowed_tables=[],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["violations"][0]["code"], "missing_allowed_tables")

    async def test_explanation_uses_injected_langchain_llm(self):
        llm = FakeLLM()
        result = await explain_result(
            question="show gmv",
            sql="SELECT 100 AS gmv",
            rows=[{"gmv": 100}],
            metrics_result={"metrics": [{"display_name": "GMV"}]},
            llm=llm,
        )

        self.assertEqual(result["explanation"], "GMV is 100.")
        self.assertIn("show gmv", llm.calls[0]["prompt"])
        self.assertIn("100", llm.calls[0]["prompt"])

    async def test_execute_sql_reads_environment_without_runtime_dotenv_load(self):
        execute_sql_module = importlib.import_module("graph.tools.execute_sql")

        class FakeConnection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql):
                self.calls.append(("execute", sql))

            async def fetch(self, sql):
                self.calls.append(("fetch", sql))
                return [{"value": 1}]

            async def close(self):
                self.calls.append(("close",))

        conn = FakeConnection()
        validation = {
            "ok": True,
            "normalized_sql": "SELECT 1 LIMIT 1000",
            "violations": [],
            "message": "valid",
        }
        with mock.patch.dict(
            os.environ,
            {"DATABASE_URL": "postgresql://example/db"},
            clear=True,
        ), mock.patch.object(
            execute_sql_module,
            "load_dotenv",
            create=True,
            side_effect=AssertionError("runtime dotenv load is blocking"),
        ), mock.patch.object(
            execute_sql_module,
            "validate_sql",
            new=mock.AsyncMock(return_value=validation),
        ), mock.patch.object(
            execute_sql_module,
            "_connect",
            new=mock.AsyncMock(return_value=conn),
        ) as connect:
            result = await execute_sql_module.execute_sql(
                "SELECT 1",
                "demo",
                allowed_tables=["orders"],
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"], [{"value": 1}])
        connect.assert_awaited_once_with("postgresql://example/db")
        self.assertEqual(conn.calls[-1], ("close",))

    async def test_execute_sql_connect_uses_preloaded_asyncpg_driver(self):
        execute_sql_module = importlib.import_module("graph.tools.execute_sql")
        original_import = builtins.__import__

        def reject_runtime_dependency_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "asyncpg":
                raise AssertionError("runtime dependency import is blocking")
            return original_import(name, globals, locals, fromlist, level)

        fake_asyncpg = types.SimpleNamespace(
            connect=mock.AsyncMock(return_value="connection")
        )
        with mock.patch.object(
            execute_sql_module,
            "asyncpg",
            fake_asyncpg,
            create=True,
        ), mock.patch(
            "builtins.__import__",
            side_effect=reject_runtime_dependency_import,
        ):
            result = await execute_sql_module._connect("postgresql://example/db")

        self.assertEqual(result, "connection")
        fake_asyncpg.connect.assert_awaited_once_with(
            "postgresql://example/db",
            ssl=False,
        )


if __name__ == "__main__":
    unittest.main()
