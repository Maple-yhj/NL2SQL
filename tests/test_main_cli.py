import contextlib
import io
import unittest
from unittest import mock

import main


class MainCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_prints_langgraph_result(self):
        result = {
            "ok": True,
            "question": "show gmv",
            "contextualized_question": "show gmv",
            "conversation_id": "conv-1",
            "user_id": "user-1",
            "tenant_id": "demo",
            "intent": {"metrics": ["gmv"]},
            "sql": "SELECT SUM(amount) FROM orders LIMIT 100",
            "rows": [{"gmv": 100}],
            "answer": "GMV is 100.",
            "error": "",
            "trace": [],
        }
        output = io.StringIO()
        with mock.patch.object(
            main,
            "run_nl2sql",
            new=mock.AsyncMock(return_value=result),
        ), contextlib.redirect_stdout(output):
            await main.run_once("show gmv", execute=True)

        text = output.getvalue()
        self.assertIn("[intent]", text)
        self.assertIn("[sql]", text)
        self.assertIn("[rows]", text)
        self.assertIn("[answer]", text)

    async def test_run_once_forwards_conversation_identity(self):
        result = {
            "ok": True,
            "question": "show gmv",
            "contextualized_question": "show gmv",
            "conversation_id": "conv-1",
            "user_id": "user-1",
            "tenant_id": "demo",
            "intent": {"metrics": ["gmv"]},
            "sql": "SELECT 1",
            "rows": [],
            "answer": "",
            "error": "",
            "trace": [],
        }
        with mock.patch.object(
            main,
            "run_nl2sql",
            new=mock.AsyncMock(return_value=result),
        ) as runner:
            await main.run_once(
                "show gmv",
                tenant_id="demo",
                execute=False,
                conversation_id="conv-1",
                user_id="user-1",
            )

        runner.assert_awaited_once_with(
            "show gmv",
            tenant_id="demo",
            execute=False,
            conversation_id="conv-1",
            user_id="user-1",
        )


if __name__ == "__main__":
    unittest.main()
