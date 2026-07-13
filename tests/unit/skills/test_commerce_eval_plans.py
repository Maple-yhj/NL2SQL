from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
PACK_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.runtime import load_domain_pack
from data_agent.runtime.packs import DomainEvalCalculation
from data_agent.skills import (
    CommercePlanValidator,
    ResultShape,
    logical_plan_from_eval_case,
)


class CommerceEvalPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.domain_pack = load_domain_pack(PACK_ROOT)
        cls.cases = {case.id: case for case in cls.domain_pack.spec.evals}
        cls.plans = {
            case_id: logical_plan_from_eval_case(case, cls.domain_pack)
            for case_id, case in cls.cases.items()
        }
        cls.validator = CommercePlanValidator()

    def test_all_48_evals_convert_to_valid_plans_and_preserve_semantic_oracles(self) -> None:
        self.assertEqual(len(self.cases), 48)
        for case_id, case in self.cases.items():
            with self.subTest(case=case_id):
                plan = self.plans[case_id]
                result = self.validator.validate(plan, self.domain_pack)
                self.assertTrue(result.valid, result.issues)
                expected_analysis_type = (
                    "comparison"
                    if case_id == "commerce.metric_006"
                    else case.analysis_type
                )
                self.assertEqual(plan.analysis_type, expected_analysis_type)
                self.assertEqual(plan.metrics, case.expected_metrics)
                self.assertEqual(plan.entities, case.expected_entities)
                self.assertEqual(plan.dimensions, case.expected_dimensions)
                self.assertEqual(plan.fields, case.expected_fields)
                self.assertEqual(plan.expected_grain, case.expected_grain)
                self.assertEqual(plan.context.mode, case.context.mode)
                self.assertEqual(plan.context.tenant_scope, case.context.tenant_scope)
                self.assertEqual(plan.context.prior_question, case.context.prior_question)
                self.assertEqual(plan.context.preserve, case.context.preserve)

                self.assertEqual(
                    tuple((item.ref, item.operator, item.value) for item in plan.filters),
                    tuple((item.ref, item.operator, item.value) for item in case.filters),
                )
                self.assertEqual(
                    tuple((item.ref, item.operator, item.value) for item in plan.having),
                    tuple((item.ref, item.operator, item.value) for item in case.having),
                )
                self.assertEqual(
                    tuple(
                        (
                            item.id,
                            item.operation,
                            item.inputs,
                            item.partition_by,
                        )
                        for item in plan.derived_calculations
                    ),
                    self._expected_typed_calculations(case.calculations),
                )
                self.assertEqual(
                    tuple(
                        (item.ref, item.direction)
                        for item in plan.ordering[: len(case.ordering)]
                    ),
                    tuple((item.ref, item.direction) for item in case.ordering),
                )
                if case.limit is not None:
                    self.assertEqual(plan.limit, case.limit)
                if case.time is None:
                    self.assertIsNone(plan.time_range)
                    self.assertIsNone(plan.time_grain)
                else:
                    self.assertEqual(plan.time_range.field, case.time.field)
                    self.assertEqual(plan.time_range.start, case.time.start)
                    self.assertEqual(plan.time_range.end, case.time.end)
                    self.assertEqual(plan.time_grain, case.time.grain)

    @staticmethod
    def _expected_typed_calculations(
        calculations: tuple[DomainEvalCalculation, ...],
    ) -> tuple[tuple[object, ...], ...]:
        expected: list[tuple[object, ...]] = []
        for item in calculations:
            if item.operation == "count_distinct" and len(item.inputs) > 1:
                composite_id = f"{item.id}_key"
                expected.append((composite_id, "composite_key", item.inputs, ()))
                expected.append((item.id, "count_distinct", (composite_id,), item.partition_by))
                continue
            expected.append((item.id, item.operation, item.inputs, item.partition_by))
        return tuple(expected)

    def test_each_analysis_shape_keeps_a_real_semantic_oracle(self) -> None:
        representatives = {
            "metric": ("commerce.join_006", ResultShape.TABLE, "commerce.payment_amount_total"),
            "trend": ("commerce.metric_001", ResultShape.TIME_SERIES, "commerce.gmv"),
            "ranking": ("commerce.join_005", ResultShape.RANKING, "commerce.review_count"),
            "detail": ("commerce.detail_003", ResultShape.DETAIL, "commerce.freight_price_difference"),
            "comparison": ("commerce.metric_011", ResultShape.TABLE, "commerce.yearly_gmv_change"),
            "cross_tab": ("commerce.metric_012", ResultShape.CROSS_TAB, "commerce.gmv"),
            "distribution": ("commerce.review_001", ResultShape.DISTRIBUTION, "commerce.review_count"),
            "derived": ("commerce.detail_006", ResultShape.DETAIL, "commerce.product_volume"),
            "follow_up": ("commerce.followup_001", ResultShape.TIME_SERIES, "commerce.gmv"),
            "tenant_scoped": ("commerce.tenant_001", ResultShape.SCALAR, "commerce.gmv"),
        }

        self.assertEqual(set(representatives), {case.analysis_type for case in self.cases.values()})
        for analysis_type, (case_id, expected_shape, semantic_ref) in representatives.items():
            with self.subTest(analysis_type=analysis_type, case=case_id):
                plan = self.plans[case_id]
                serialized = plan.canonical_json()
                self.assertEqual(plan.analysis_type, analysis_type)
                self.assertEqual(plan.result_shape, expected_shape)
                self.assertIn(semantic_ref, serialized)
                self.assertTrue(
                    plan.metrics or plan.fields or plan.derived_calculations
                )
                self.assertTrue(
                    self.validator.validate(plan, self.domain_pack).valid
                )

        self.assertTrue(self.plans["commerce.metric_010"].window_specs)
        self.assertTrue(self.plans["commerce.join_005"].having)
        self.assertTrue(self.plans["commerce.metric_012"].grain_alignment)
        self.assertEqual(
            self.plans["commerce.followup_001"].context.preserve,
            ("metrics", "time_range"),
        )
        self.assertEqual(
            self.plans["commerce.tenant_001"].context.tenant_scope,
            "seller",
        )

    def test_unbounded_eval_detail_gets_an_explicit_safe_limit_and_order(self) -> None:
        plan = self.plans["commerce.detail_005"]

        self.assertEqual(plan.limit, 1000)
        self.assertTrue(plan.ordering)
        self.assertIn("bounded_detail_default", plan.assumptions)
        self.assertTrue(self.validator.validate(plan, self.domain_pack).valid)

    def test_generated_plans_contain_only_logical_contract_fields(self) -> None:
        forbidden_keys = {
            "relation",
            "table",
            "column",
            "schema",
            "database",
            "connector",
            "credential",
            "connectionref",
            "sql",
            "rawsql",
        }

        def normalized(value: object) -> str:
            return re.sub(r"[^a-z0-9]", "", str(value).lower())

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    self.assertNotIn(normalized(key), forbidden_keys)
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        for case_id, plan in self.plans.items():
            with self.subTest(case=case_id):
                walk(plan.model_dump(mode="json", by_alias=True))


if __name__ == "__main__":
    unittest.main()
