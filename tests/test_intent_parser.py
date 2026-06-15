import unittest
from datetime import datetime, timedelta, timezone

from engine.intent_parser import build_intent_system, parse_intent


class FakeLLM:
    async def complete(self, prompt, system="", max_output_tokens=2048):
        return '{"metrics":["gmv"],"time_range":{},"dimensions":["region"],"filters":[]}'


class IntentParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_intent_returns_typed_intent(self):
        intent = await parse_intent("GMV by region", llm=FakeLLM())
        self.assertEqual(intent.metrics, ["gmv"])
        self.assertEqual(intent.dimensions, ["region"])

    def test_system_prompt_contains_local_date_reference(self):
        now = datetime(2026, 6, 15, 9, 30, tzinfo=timezone(timedelta(hours=8)))
        prompt = build_intent_system(now)
        self.assertIn("2026-06-15", prompt)


if __name__ == "__main__":
    unittest.main()
