import unittest

from engine.models import QueryIntent
from engine.plan_models import (
    AnalysisType,
    ExecutionMode,
    PlanDSL,
    build_execution_graph,
    format_plan_context,
    plan_search_query,
)


class PlanModelTests(unittest.TestCase):
    def test_single_metric_intent_builds_single_metric_plan(self):
        intent = QueryIntent(metrics=["gmv"], time_range={"start": "2026-06-01", "end": "2026-06-30"})

        plan = PlanDSL.from_intent(intent, question="show gmv last month")

        self.assertEqual(plan.analysis_type, AnalysisType.SINGLE_METRIC)
        self.assertEqual([metric.name for metric in plan.metrics], ["gmv"])
        self.assertEqual(plan.time_range["start"], "2026-06-01")
        self.assertEqual(plan.to_query_intent(), intent)

    def test_explicit_limit_round_trips_through_plan_result_shape(self):
        intent = QueryIntent(metrics=["gmv"], dimensions=["customer_city"], limit=20)

        plan = PlanDSL.from_intent(intent, question="show top customer cities, return 20")

        self.assertEqual(plan.result_shape.limit, 20)
        self.assertEqual(plan.to_query_intent(), intent)

    def test_dimensions_build_multi_dimensional_plan(self):
        intent = QueryIntent(metrics=["gmv"], dimensions=["region", "category"])

        plan = PlanDSL.from_intent(intent, question="show gmv by region and category")

        self.assertEqual(plan.analysis_type, AnalysisType.MULTI_DIMENSIONAL)
        self.assertEqual([dimension.name for dimension in plan.dimensions], ["region", "category"])

    def test_trend_question_builds_trend_plan_with_default_time_grain(self):
        intent = QueryIntent(metrics=["orders"], dimensions=["paid_date"])

        plan = PlanDSL.from_intent(intent, question="show order trend by month")

        self.assertEqual(plan.analysis_type, AnalysisType.TREND)
        self.assertEqual(plan.time_grain, "month")

    def test_plan_from_dict_preserves_future_operations_and_metadata(self):
        plan = PlanDSL.from_dict(
            {
                "version": "1",
                "analysis_type": "trend",
                "metrics": [{"name": "gmv", "aggregation": "sum"}],
                "dimensions": [{"name": "paid_date", "role": "time"}],
                "time_grain": "week",
                "operations": [{"id": "compare_previous_period", "kind": "derived_metric"}],
                "metadata": {"planner": "future-dynamic"},
            }
        )

        self.assertEqual(plan.analysis_type, AnalysisType.TREND)
        self.assertEqual(plan.operations[0]["id"], "compare_previous_period")
        self.assertEqual(plan.metadata["planner"], "future-dynamic")

    def test_execution_graph_uses_fixed_mode_with_dynamic_ready_steps(self):
        plan = PlanDSL.from_intent(QueryIntent(metrics=["gmv"]), question="show gmv")

        graph = build_execution_graph(plan, execute=True)

        self.assertEqual(graph.mode, ExecutionMode.FIXED_DAG)
        self.assertEqual([step.id for step in graph.steps], [
            "metric_context",
            "schema_context",
            "sql_generation",
            "sql_validation",
            "sql_execution",
            "result_explanation",
        ])
        self.assertEqual(graph.steps[2].depends_on, ["metric_context", "schema_context"])
        self.assertEqual(graph.steps[2].tool, "generate_sql")

    def test_plan_context_is_serializable_for_prompt_and_search(self):
        plan = PlanDSL.from_intent(
            QueryIntent(metrics=["gmv"], dimensions=["region"], filters=["paid orders only"]),
            question="show gmv by region",
        )
        graph = build_execution_graph(plan, execute=False)

        context = format_plan_context(plan=plan, execution_graph=graph)
        query = plan_search_query(question="show gmv by region", plan=plan)

        self.assertIn('"analysis_type": "multi_dimensional"', context)
        self.assertIn("gmv", query)
        self.assertIn("region", query)


if __name__ == "__main__":
    unittest.main()
