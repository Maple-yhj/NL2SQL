import unittest

from engine.models import QueryIntent
from engine.planner import plan_query
from engine.plan_models import AnalysisType


class InvalidJsonLLM:
    async def complete(self, prompt, system="", max_output_tokens=2048):
        return "not json"


class JsonLLM:
    async def complete(self, prompt, system="", max_output_tokens=2048):
        return '{"analysis_type":"trend","metrics":[{"name":"gmv"}],"dimensions":[{"name":"paid_date","role":"time"}],"time_grain":"day"}'


class JsonWithoutLimitLLM:
    async def complete(self, prompt, system="", max_output_tokens=2048):
        return '{"analysis_type":"multi_dimensional","metrics":[{"name":"gmv"}],"dimensions":[{"name":"customer_city"}]}'


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_query_falls_back_to_intent_when_llm_output_is_invalid(self):
        bundle = await plan_query(
            question="show gmv by region",
            intent=QueryIntent(metrics=["gmv"], dimensions=["region"]),
            llm=InvalidJsonLLM(),
            execute=False,
        )

        self.assertEqual(bundle.plan.analysis_type, AnalysisType.MULTI_DIMENSIONAL)
        self.assertEqual(bundle.intent.metrics, ["gmv"])
        self.assertEqual(bundle.execution_graph.steps[-1].id, "sql_validation")
        self.assertIn("planner fallback", bundle.message)

    async def test_plan_query_uses_valid_llm_plan(self):
        bundle = await plan_query(
            question="show gmv trend",
            intent=QueryIntent(metrics=["gmv"]),
            llm=JsonLLM(),
            execute=True,
        )

        self.assertEqual(bundle.plan.analysis_type, AnalysisType.TREND)
        self.assertEqual(bundle.plan.time_grain, "day")
        self.assertEqual(bundle.intent.dimensions, ["paid_date"])
        self.assertEqual(bundle.execution_graph.steps[-1].id, "result_explanation")

    async def test_plan_query_preserves_intent_limit_when_planner_omits_it(self):
        bundle = await plan_query(
            question="show top customer cities, return 20",
            intent=QueryIntent(metrics=["gmv"], dimensions=["customer_city"], limit=20),
            llm=JsonWithoutLimitLLM(),
            execute=False,
        )

        self.assertEqual(bundle.plan.result_shape.limit, 20)
        self.assertEqual(bundle.intent.limit, 20)


if __name__ == "__main__":
    unittest.main()
