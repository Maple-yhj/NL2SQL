import unittest
from datetime import datetime, timezone, timedelta

from catalog.loader import load_schema_catalog, load_schema_snippet
from core.structured_output import extract_json_object
from engine.executor import assert_readonly_sql, ensure_limit
from engine.intent_parser import build_intent_system, parse_intent
from engine.metrics import MetricRegistry
from engine.sql_generator import generate_sql


class FakeLLM:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def complete(self, prompt, system="", **kwargs):
        self.calls.append({"prompt": prompt, "system": system, **kwargs})
        return self.text


class P1EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_intent_extracts_json_from_gemini_text(self):
        llm = FakeLLM(
            """
            <o>
            {
              "metrics": ["gmv"],
              "time_range": {"start": "2026-04-01", "end": "2026-04-30"},
              "dimensions": ["region"],
              "filters": ["status = paid"]
            }
            </o>
            """
        )

        intent = await parse_intent("按地区看本月 GMV", llm=llm)

        self.assertEqual(intent.metrics, ["gmv"])
        self.assertEqual(intent.dimensions, ["region"])
        self.assertEqual(intent.time_range["start"], "2026-04-01")
        self.assertIn("JSON", llm.calls[0]["system"])

    def test_intent_system_uses_local_time_as_relative_date_base(self):
        now = datetime(2026, 4, 28, 9, 30, tzinfo=timezone(timedelta(hours=8)))

        system = build_intent_system(now)

        self.assertIn("Local system datetime: 2026-04-28T09:30:00+08:00", system)
        self.assertIn("Local system date: 2026-04-28", system)
        self.assertIn("relative dates", system)

    async def test_generate_sql_uses_metrics_and_schema_context(self):
        catalog = load_schema_catalog("schema_catalog.json")
        registry = MetricRegistry.default()
        llm = FakeLLM("```sql\nselect region, sum(amount) as gmv from orders group by region\n```")

        sql = await generate_sql(
            question="按地区看 GMV",
            intent=await parse_intent(
                "按地区看 GMV",
                llm=FakeLLM('{"metrics":["gmv"],"time_range":{},"dimensions":["region"],"filters":[]}'),
            ),
            catalog=catalog,
            metrics=registry,
            llm=llm,
        )

        self.assertEqual(
            sql,
            "select region, sum(amount) as gmv from orders group by region limit 100",
        )
        self.assertIn("gmv", llm.calls[0]["prompt"])
        self.assertIn("orders", llm.calls[0]["prompt"])

    def test_readonly_sql_validator_blocks_dml(self):
        assert_readonly_sql("select * from orders limit 10")

        with self.assertRaises(ValueError):
            assert_readonly_sql("delete from orders")

    def test_ensure_limit_appends_missing_limit(self):
        self.assertEqual(ensure_limit("select * from orders"), "select * from orders limit 100")
        self.assertEqual(ensure_limit("select * from orders limit 5"), "select * from orders limit 5")

    def test_schema_snippet_filters_tables(self):
        catalog = load_schema_catalog("schema_catalog.json")
        snippet = load_schema_snippet(catalog, ["orders"])

        self.assertIn("orders", snippet)
        self.assertIn("amount", snippet)
        self.assertNotIn("products", snippet)

    def test_extract_json_object_supports_tags_and_fences(self):
        self.assertEqual(extract_json_object("<o>{\"a\": 1}</o>"), {"a": 1})
        self.assertEqual(extract_json_object("```json\n{\"b\": 2}\n```"), {"b": 2})


if __name__ == "__main__":
    unittest.main()
