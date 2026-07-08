import unittest
from datetime import datetime, timedelta, timezone

from engine.intent_parser import build_intent_system, parse_intent


class FakeLLM:
    def __init__(self, payload=None):
        self.payload = payload or '{"metrics":["gmv"],"time_range":{},"dimensions":["region"],"filters":[]}'

    async def complete(self, prompt, system="", max_output_tokens=2048):
        return self.payload


class IntentParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_intent_returns_typed_intent(self):
        intent = await parse_intent("GMV by region", llm=FakeLLM())
        self.assertEqual(intent.metrics, ["gmv"])
        self.assertEqual(intent.dimensions, ["region"])

    async def test_parse_intent_extracts_explicit_result_limit(self):
        intent = await parse_intent(
            "客户最多的州和城市分别有哪些？返回前 20 个城市",
            llm=FakeLLM(
                '{"metrics":[],"time_range":{},"dimensions":["customer_state","customer_city"],"filters":[]}'
            ),
        )

        self.assertEqual(intent.limit, 20)

    async def test_parse_intent_uses_return_limit_not_threshold_count(self):
        intent = await parse_intent(
            "哪些商品品类平均评价分最低？至少有 100 条评价，返回前 10 个",
            llm=FakeLLM(
                '{"metrics":["avg_review_score"],"time_range":{},"dimensions":["product_category_name"],"filters":["至少有 100 条评价"]}'
            ),
        )

        self.assertEqual(intent.limit, 10)
        self.assertIn("至少有 100 条评价", intent.filters)

    async def test_parse_intent_does_not_treat_threshold_as_result_limit(self):
        intent = await parse_intent(
            "哪些商品品类平均评价分最低？至少有 100 条评价",
            llm=FakeLLM(
                '{"metrics":["avg_review_score"],"time_range":{},"dimensions":["product_category_name"],"filters":["至少有 100 条评价"]}'
            ),
        )

        self.assertIsNone(intent.limit)

    def test_system_prompt_contains_local_date_reference(self):
        now = datetime(2026, 6, 15, 9, 30, tzinfo=timezone(timedelta(hours=8)))
        prompt = build_intent_system(now)
        self.assertIn("2026-06-15", prompt)
        self.assertIn('"limit"', prompt)


if __name__ == "__main__":
    unittest.main()
