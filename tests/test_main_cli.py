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


if __name__ == "__main__":
    unittest.main()
