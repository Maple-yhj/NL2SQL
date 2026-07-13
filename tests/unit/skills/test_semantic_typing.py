from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
PACK_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.runtime import load_domain_pack
from data_agent.skills import (
    CommercePlanValidator,
    DerivedCalculation,
    LogicalFilter,
    PlanValidationCode,
    logical_plan_from_eval_case,
)


class SemanticTypingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.domain_pack = load_domain_pack(PACK_ROOT)
        cls.cases = {case.id: case for case in cls.domain_pack.spec.evals}
        cls.validator = CommercePlanValidator()

    def _plan(self, case_id: str):
        return logical_plan_from_eval_case(
            self.cases[case_id],
            self.domain_pack,
        )

    def _codes(self, plan) -> tuple[PlanValidationCode, ...]:
        return tuple(
            issue.code
            for issue in self.validator.validate(plan, self.domain_pack).issues
        )

    def test_numeric_aggregations_reject_string_inputs(self) -> None:
        base = self._plan("commerce.join_006")
        invalid_sum = base.derived_calculations[0].model_copy(
            update={"inputs": ("commerce.Payment.method",)}
        )
        invalid_average = invalid_sum.model_copy(update={"operation": "average"})

        for operation, calculation in (
            ("sum", invalid_sum),
            ("average", invalid_average),
        ):
            with self.subTest(operation=operation):
                plan = base.model_copy(
                    update={
                        "derived_calculations": (
                            calculation,
                            *base.derived_calculations[1:],
                        )
                    }
                )
                self.assertIn(
                    PlanValidationCode.INVALID_CALCULATION,
                    self._codes(plan),
                )

    def test_unary_aggregates_reject_two_or_more_inputs(self) -> None:
        base = self._plan("commerce.join_006")
        variants = (
            ("sum", ("commerce.Payment.amount", "commerce.Payment.installment_count")),
            ("average", ("commerce.Payment.amount", "commerce.Payment.installment_count")),
            ("count", ("commerce.Payment.order_id", "commerce.Payment.method")),
            ("count_distinct", ("commerce.Payment.order_id", "commerce.Payment.method")),
        )

        for operation, inputs in variants:
            with self.subTest(operation=operation):
                calculation = base.derived_calculations[0].model_copy(
                    update={"operation": operation, "inputs": inputs}
                )
                plan = base.model_copy(
                    update={
                        "derived_calculations": (
                            calculation,
                            *base.derived_calculations[1:],
                        )
                    }
                )
                self.assertIn(
                    PlanValidationCode.INVALID_CALCULATION,
                    self._codes(plan),
                )

    def test_unary_aggregate_cannot_use_an_empty_implicit_count_all(self) -> None:
        base = self._plan("commerce.join_006")
        payload = base.model_dump(mode="json", by_alias=True)
        payload["derivedCalculations"][0]["operation"] = "count"
        payload["derivedCalculations"][0]["inputs"] = []

        result = self.validator.validate(payload, self.domain_pack)

        self.assertFalse(result.valid)
        self.assertIn(
            PlanValidationCode.INVALID_PLAN_CONTRACT,
            tuple(issue.code for issue in result.issues),
        )

    def test_multi_field_distinct_uses_an_explicit_composite_key_ast(self) -> None:
        plan = self._plan("commerce.geo_003")

        self.assertEqual(len(plan.derived_calculations), 2)
        key, count = plan.derived_calculations
        self.assertEqual(key.operation, "composite_key")
        self.assertEqual(
            key.inputs,
            (
                "commerce.GeoLocation.latitude",
                "commerce.GeoLocation.longitude",
            ),
        )
        self.assertEqual(count.operation, "count_distinct")
        self.assertEqual(count.inputs, (key.id,))
        self.assertTrue(self.validator.validate(plan, self.domain_pack).valid)

    def test_date_difference_requires_two_temporal_inputs(self) -> None:
        base = self._plan("commerce.logistics_003")
        invalid = base.derived_calculations[0].model_copy(
            update={
                "inputs": (
                    "commerce.Order.status",
                    "commerce.Order.order_id",
                )
            }
        )
        plan = base.model_copy(
            update={
                "derived_calculations": (
                    invalid,
                    *base.derived_calculations[1:],
                )
            }
        )

        self.assertIn(
            PlanValidationCode.INVALID_CALCULATION,
            self._codes(plan),
        )

    def test_filter_operator_and_literal_types_follow_canonical_field_types(self) -> None:
        base = self._plan("commerce.detail_002")
        invalid_filters = (
            LogicalFilter(
                ref="commerce.OrderItem.item_price",
                operator="contains",
                value="1",
            ),
            LogicalFilter(
                ref="commerce.OrderItem.item_price",
                operator="gt",
                value="expensive",
            ),
            LogicalFilter(
                ref="commerce.OrderItem.item_sequence",
                operator="eq",
                value=1.5,
            ),
            LogicalFilter(
                ref="commerce.OrderItem.seller_id",
                operator="in",
                value=("seller-1", 2),
            ),
        )

        for predicate in invalid_filters:
            with self.subTest(ref=predicate.ref, operator=predicate.operator):
                plan = base.model_copy(update={"filters": (predicate,)})
                self.assertIn(
                    PlanValidationCode.PREDICATE_TYPE_MISMATCH,
                    self._codes(plan),
                )

    def test_temporal_filter_requires_an_iso_value_and_valid_operator(self) -> None:
        base = self._plan("commerce.detail_004")
        invalid = LogicalFilter(
            ref="commerce.Order.purchased_at",
            operator="gte",
            value="not-a-date",
        )
        valid = invalid.model_copy(update={"value": "2018-01-01T00:00:00"})

        self.assertIn(
            PlanValidationCode.PREDICATE_TYPE_MISMATCH,
            self._codes(base.model_copy(update={"filters": (invalid,)})),
        )
        self.assertNotIn(
            PlanValidationCode.PREDICATE_TYPE_MISMATCH,
            self._codes(base.model_copy(update={"filters": (valid,)})),
        )

    def test_having_literal_type_is_checked_against_aggregate_output(self) -> None:
        base = self._plan("commerce.join_005")
        invalid_having = base.having[0].model_copy(update={"value": "many"})
        plan = base.model_copy(update={"having": (invalid_having,)})

        self.assertIn(
            PlanValidationCode.PREDICATE_TYPE_MISMATCH,
            self._codes(plan),
        )

    def test_growth_window_requires_a_numeric_calculation_input(self) -> None:
        base = self._plan("commerce.metric_011")
        invalid_growth = base.derived_calculations[0].model_copy(
            update={"inputs": ("commerce.Seller.state",)}
        )
        plan = base.model_copy(
            update={"derived_calculations": (invalid_growth,)}
        )

        self.assertIn(
            PlanValidationCode.INVALID_CALCULATION,
            self._codes(plan),
        )

    def test_raw_expression_and_unsupported_division_never_enter_typed_ast(self) -> None:
        base = self._plan("commerce.detail_006")
        payload = base.model_dump(mode="json", by_alias=True)
        payload["derivedCalculations"][0]["operation"] = "divide"
        payload["derivedCalculations"][0]["expression"] = " / ".join(
            ("commerce.Product.length_centimeters", "0")
        )

        result = self.validator.validate(payload, self.domain_pack)

        self.assertFalse(result.valid)
        self.assertTrue(
            {
                PlanValidationCode.RAW_SQL_FORBIDDEN,
                PlanValidationCode.INVALID_PLAN_CONTRACT,
            }
            & set(issue.code for issue in result.issues)
        )

    def test_calculation_alias_must_use_domain_namespace_and_not_collide(self) -> None:
        base = self._plan("commerce.join_006")
        cross_namespace = base.derived_calculations[0].model_copy(
            update={"id": "external.payment_total"}
        )
        relationship_collision = base.derived_calculations[0].model_copy(
            update={"id": "commerce.payment_order"}
        )

        for name, calculation, expected in (
            (
                "namespace",
                cross_namespace,
                PlanValidationCode.INVALID_CALCULATION,
            ),
            (
                "relationship_collision",
                relationship_collision,
                PlanValidationCode.DUPLICATE_REFERENCE,
            ),
        ):
            with self.subTest(case=name):
                plan = base.model_copy(
                    update={
                        "derived_calculations": (
                            calculation,
                            *base.derived_calculations[1:],
                        )
                    }
                )
                self.assertIn(expected, self._codes(plan))

    def test_window_alias_shares_the_domain_logical_namespace(self) -> None:
        base = self._plan("commerce.metric_010")
        calculation_collision = base.window_specs[0].model_copy(
            update={"id": base.derived_calculations[0].id}
        )
        cross_namespace = base.window_specs[0].model_copy(
            update={"id": "external.monthly_window"}
        )

        for name, window, expected in (
            (
                "calculation_collision",
                calculation_collision,
                PlanValidationCode.DUPLICATE_REFERENCE,
            ),
            (
                "namespace",
                cross_namespace,
                PlanValidationCode.INVALID_WINDOW,
            ),
        ):
            with self.subTest(case=name):
                plan = base.model_copy(update={"window_specs": (window,)})
                self.assertIn(expected, self._codes(plan))

    def test_unknown_calculation_alias_input_is_rejected(self) -> None:
        base = self._plan("commerce.payment_004")
        invalid = DerivedCalculation(
            id="commerce.invalid_total",
            operation="sum",
            inputs=("commerce.missing_alias",),
        )
        plan = base.model_copy(update={"derived_calculations": (invalid,)})

        self.assertIn(
            PlanValidationCode.INVALID_CALCULATION,
            self._codes(plan),
        )


if __name__ == "__main__":
    unittest.main()
