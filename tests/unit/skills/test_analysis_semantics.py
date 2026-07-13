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
    CrossTabSpec,
    LogicalOrdering,
    PlanContext,
    PlanValidationCode,
    RankingSpec,
    ResultShape,
    SeriesAxis,
    TimeRange,
    logical_plan_from_eval_case,
)


class AnalysisSemanticsTests(unittest.TestCase):
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

    def test_non_temporal_dimension_cannot_masquerade_as_time_series(self) -> None:
        base = self._plan("commerce.review_001")
        relabeled = base.model_copy(
            update={
                "analysis_type": "trend",
                "result_shape": "time_series",
            }
        )

        self.assertIn(
            PlanValidationCode.ANALYSIS_SEMANTICS_MISMATCH,
            self._codes(relabeled),
        )

    def test_time_series_requires_canonical_temporal_axis_and_time_grain(self) -> None:
        base = self._plan("commerce.metric_001")
        missing_time = base.model_copy(
            update={"time_range": None, "time_grain": None}
        )
        wrong_type = self._plan("commerce.review_001").model_copy(
            update={
                "analysis_type": "trend",
                "series_axis": SeriesAxis(
                    kind="time",
                    field="commerce.Review.score",
                    time_grain="month",
                ),
                "time_range": TimeRange(
                    field="commerce.Review.score",
                    start="2018-01-01",
                    end="2019-01-01",
                ),
                "time_grain": "month",
                "result_shape": "time_series",
            }
        )

        for name, plan in (("missing", missing_time), ("wrong_type", wrong_type)):
            with self.subTest(case=name):
                self.assertIn(
                    PlanValidationCode.ANALYSIS_SEMANTICS_MISMATCH,
                    self._codes(plan),
                )

    def test_ordered_numeric_series_is_explicit_and_not_called_time_series(self) -> None:
        plan = self._plan("commerce.payment_002")

        self.assertEqual(plan.series_axis.kind, "numeric")
        self.assertEqual(
            plan.series_axis.field,
            "commerce.Payment.installment_count",
        )
        self.assertEqual(plan.result_shape, ResultShape.TABLE)
        self.assertTrue(self.validator.validate(plan, self.domain_pack).valid)

    def test_top_n_requires_matching_measure_order_and_limit(self) -> None:
        base = self._plan("commerce.metric_002")
        self.assertEqual(base.ranking.mode, "top_n")
        mutations = (
            base.model_copy(update={"limit": None}),
            base.model_copy(update={"ordering": ()}),
            base.model_copy(
                update={
                    "ranking": RankingSpec(
                        mode="top_n",
                        measure="commerce.Seller.seller_id",
                    )
                }
            ),
        )

        for plan in mutations:
            with self.subTest(plan=plan.stable_hash()):
                self.assertIn(
                    PlanValidationCode.ANALYSIS_SEMANTICS_MISMATCH,
                    self._codes(plan),
                )

    def test_limited_aggregate_result_cannot_omit_top_n_specification(self) -> None:
        base = self._plan("commerce.metric_012")
        self.assertEqual(base.ranking.mode, "top_n")
        hidden_top_n = base.model_copy(update={"ranking": None})

        self.assertIn(
            PlanValidationCode.ANALYSIS_SEMANTICS_MISMATCH,
            self._codes(hidden_top_n),
        )

    def test_full_ranking_is_explicit_and_may_omit_limit(self) -> None:
        plan = self._plan("commerce.metric_004")

        self.assertEqual(plan.ranking.mode, "full")
        self.assertIsNone(plan.limit)
        self.assertTrue(self.validator.validate(plan, self.domain_pack).valid)

    def test_detail_shape_rejects_aggregate_outputs(self) -> None:
        base = self._plan("commerce.detail_001")
        invalid = base.model_copy(update={"metrics": ("commerce.gmv",)})

        self.assertIn(
            PlanValidationCode.ANALYSIS_SEMANTICS_MISMATCH,
            self._codes(invalid),
        )

    def test_metric_cross_tab_and_distribution_require_real_output_structure(self) -> None:
        metric_with_fields = self._plan("commerce.metric_001").model_copy(
            update={"fields": ("commerce.OrderItem.item_price",)}
        )
        empty_cross_tab = self._plan("commerce.metric_006").model_copy(
            update={"dimensions": (), "expected_grain": ()}
        )
        empty_distribution = self._plan("commerce.review_001").model_copy(
            update={"dimensions": (), "expected_grain": ()}
        )

        for name, plan in (
            ("metric_fields", metric_with_fields),
            ("cross_tab", empty_cross_tab),
            ("distribution", empty_distribution),
        ):
            with self.subTest(case=name):
                self.assertIn(
                    PlanValidationCode.ANALYSIS_SEMANTICS_MISMATCH,
                    self._codes(plan),
                )

    def test_tenant_scoped_analysis_requires_tenant_context_and_filter(self) -> None:
        base = self._plan("commerce.tenant_001")
        missing_filter = base.model_copy(update={"filters": ()})
        all_tenants = base.model_copy(
            update={"context": PlanContext(tenant_scope="all")}
        )

        for plan in (missing_filter, all_tenants):
            with self.subTest(plan=plan.stable_hash()):
                self.assertIn(
                    PlanValidationCode.CONTEXT_MISMATCH,
                    self._codes(plan),
                )

    def test_follow_up_analysis_requires_prior_context_and_preservation_contract(self) -> None:
        base = self._plan("commerce.followup_001")
        invalid = base.model_copy(update={"context": PlanContext()})

        self.assertIn(
            PlanValidationCode.CONTEXT_MISMATCH,
            self._codes(invalid),
        )

    def test_temporal_top_one_trend_is_a_typed_ranking_over_a_time_axis(self) -> None:
        plan = self._plan("commerce.metric_010")

        self.assertEqual(plan.series_axis.kind, "time")
        self.assertEqual(plan.ranking.mode, "top_n")
        self.assertEqual(plan.limit, 1)
        self.assertEqual(plan.result_shape, ResultShape.RANKING)
        self.assertTrue(plan.window_specs)
        self.assertTrue(self.validator.validate(plan, self.domain_pack).valid)

    def test_sequence_window_requires_series_axis_as_its_primary_order(self) -> None:
        base = self._plan("commerce.metric_011")
        axis = base.series_axis.field
        wrong_only = base.window_specs[0].model_copy(
            update={
                "ordering": (
                    LogicalOrdering(ref="commerce.Seller.state", direction="asc"),
                )
            }
        )
        wrong_primary = base.window_specs[0].model_copy(
            update={
                "ordering": (
                    LogicalOrdering(ref="commerce.Seller.state", direction="asc"),
                    LogicalOrdering(ref=axis, direction="asc"),
                )
            }
        )

        for name, window in (("missing", wrong_only), ("not_primary", wrong_primary)):
            with self.subTest(case=name):
                plan = base.model_copy(update={"window_specs": (window,)})
                self.assertIn(
                    PlanValidationCode.INVALID_WINDOW,
                    self._codes(plan),
                )

    def test_sequence_window_declares_the_same_axis_ref_as_the_plan(self) -> None:
        plan = self._plan("commerce.metric_011")
        window = plan.window_specs[0]

        self.assertEqual(window.axis_ref, plan.series_axis.field)
        self.assertEqual(window.ordering[0].ref, window.axis_ref)

    def test_cross_tab_spec_has_two_distinct_dimension_axes(self) -> None:
        self.assertEqual(
            set(CrossTabSpec.model_fields),
            {"row_axis", "column_axis", "values"},
        )
        spec = CrossTabSpec(
            row_axis="commerce.Product.category_code",
            column_axis="commerce.Payment.method",
            values=("commerce.gmv",),
        )

        self.assertEqual(spec.row_axis, "commerce.Product.category_code")
        self.assertEqual(spec.column_axis, "commerce.Payment.method")

    def test_cross_tab_spec_rejects_non_dimension_or_incomplete_axes(self) -> None:
        with self.assertRaisesRegex(ValueError, "independent"):
            CrossTabSpec(
                row_axis="commerce.Payment.method",
                column_axis="commerce.Payment.method",
                values=("commerce.gmv",),
            )
        with self.assertRaises(ValueError):
            CrossTabSpec(
                row_axis="commerce.Payment.method",
                column_axis="commerce.gmv",
                values=("commerce.gmv",),
            )
        with self.assertRaises(ValueError):
            CrossTabSpec(
                row_axis="commerce.Product.category_code",
                column_axis="commerce.Payment.method",
                values=(),
            )

    def test_cross_tab_validator_requires_complete_axes_and_known_values(self) -> None:
        base = self._plan("commerce.metric_012")
        row_axis = base.cross_tab.row_axis
        one_dimension = base.model_copy(
            update={
                "dimensions": (row_axis,),
                "expected_grain": (row_axis,),
            }
        )
        unknown_value = base.model_copy(
            update={
                "cross_tab": base.cross_tab.model_copy(
                    update={"values": ("commerce.unknown_output",)}
                )
            }
        )

        for name, plan in (
            ("one_dimension", one_dimension),
            ("unknown_value", unknown_value),
        ):
            with self.subTest(case=name):
                self.assertIn(
                    PlanValidationCode.ANALYSIS_SEMANTICS_MISMATCH,
                    self._codes(plan),
                )

    def test_single_dimension_governed_case_is_a_comparison_table(self) -> None:
        plan = self._plan("commerce.metric_006")

        self.assertEqual(plan.analysis_type, "comparison")
        self.assertEqual(plan.result_shape, ResultShape.TABLE)
        self.assertIsNone(plan.cross_tab)
        self.assertEqual(plan.dimensions, ("commerce.Payment.method",))
        self.assertTrue(self.validator.validate(plan, self.domain_pack).valid)

    def test_true_cross_tabs_use_two_canonical_dimension_axes(self) -> None:
        expected_axes = {
            "commerce.metric_012": (
                "commerce.Product.category_code",
                "commerce.Payment.method",
            ),
            "commerce.join_007": (
                "commerce.Seller.state",
                "commerce.Customer.state",
            ),
        }

        for case_id, (row_axis, column_axis) in expected_axes.items():
            with self.subTest(case=case_id):
                plan = self._plan(case_id)
                self.assertEqual(getattr(plan.cross_tab, "row_axis", None), row_axis)
                self.assertEqual(getattr(plan.cross_tab, "column_axis", None), column_axis)
                self.assertEqual(plan.cross_tab.values, ("commerce.gmv",))
                self.assertTrue(
                    self.validator.validate(plan, self.domain_pack).valid
                )


if __name__ == "__main__":
    unittest.main()
