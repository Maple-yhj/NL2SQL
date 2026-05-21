import contextlib
import io
import unittest
from unittest import mock

import main


class MainCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_agent_prints_explanation_when_execute_enabled(self):
        result = {
            "intent": {"metrics": ["gmv"]},
            "executed_sql": "SELECT SUM(amount) FROM orders",
            "rows": [{"gmv": 100}],
            "explanation": "mock explanation",
        }

        output = io.StringIO()
        with mock.patch.object(
            main,
            "run_agent_nl2sql",
            new=mock.AsyncMock(return_value=result),
        ), contextlib.redirect_stdout(output):
            await main.run_once_agent("show gmv", execute=True)

        text = output.getvalue()
        self.assertIn("[intent]", text)
        self.assertIn("[sql]", text)
        self.assertIn("[rows]", text)
        self.assertIn("[explanation]", text)
        self.assertIn("mock explanation", text)


if __name__ == "__main__":
    unittest.main()
