import unittest
from unittest import mock

from agent import pipeline


class AgentPipelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_agent_delegates_existing_public_arguments(self):
        expected = {"ok": True, "message": "success"}
        llm = object()
        with mock.patch.object(
            pipeline,
            "run_react_nl2sql",
            new=mock.AsyncMock(return_value=expected),
        ) as run_react:
            result = await pipeline.run_agent_nl2sql(
                "show gmv",
                tenant_id="tenant-a",
                execute=True,
                llm=llm,
                dsn="postgresql://example",
                max_limit=100,
            )

        self.assertEqual(result, expected)
        run_react.assert_awaited_once_with(
            "show gmv",
            "tenant-a",
            execute=True,
            llm=llm,
            dsn="postgresql://example",
            max_limit=100,
            timeout_ms=10_000,
            max_steps=8,
        )

    async def test_run_agent_forwards_react_runtime_limits(self):
        expected = {"ok": False, "trace": [{"status": "rejected"}]}
        with mock.patch.object(
            pipeline,
            "run_react_nl2sql",
            new=mock.AsyncMock(return_value=expected),
        ) as run_react:
            result = await pipeline.run_agent_nl2sql(
                "show gmv",
                timeout_ms=1200,
                max_steps=12,
            )

        self.assertIs(result, expected)
        run_react.assert_awaited_once_with(
            "show gmv",
            "demo",
            execute=False,
            llm=None,
            dsn=None,
            max_limit=1000,
            timeout_ms=1200,
            max_steps=12,
        )


if __name__ == "__main__":
    unittest.main()
