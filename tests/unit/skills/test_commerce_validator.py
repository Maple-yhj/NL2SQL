from __future__ import annotations

import sys
import unittest
import warnings
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
PACK_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.runtime import load_domain_pack
from data_agent.skills import (
    CommercePlanValidator,
    PlanValidationCode,
    PlanValidationError,
    TimeRange,
    logical_plan_from_eval_case,
)


class CommercePlanValidatorTests(unittest.TestCase):
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

    def _codes(self, plan, pack=None) -> tuple[PlanValidationCode, ...]:
        result = self.validator.validate(plan, pack or self.domain_pack)
        return tuple(issue.code for issue in result.issues)

    def test_valid_plan_returns_stable_hash_and_empty_issues(self) -> None:
        plan = self._plan("commerce.metric_001")

        first = self.validator.validate(plan, self.domain_pack)
        second = self.validator.validate(plan, self.domain_pack)

        self.assertTrue(first.valid)
        self.assertEqual(first.issues, ())
        self.assertEqual(first, second)
        self.assertEqual(first.plan_hash, plan.stable_hash())

    def test_unknown_canonical_metric_field_and_relationship_fail_closed(self) -> None:
        base = self._plan("commerce.metric_002")
        mutations = (
            (
                base.model_copy(update={"metrics": ("commerce.missing_metric",)}),
                PlanValidationCode.UNKNOWN_METRIC,
            ),
            (
                base.model_copy(
                    update={"dimensions": ("commerce.Seller.missing_field",)}
                ),
                PlanValidationCode.UNKNOWN_FIELD,
            ),
            (
                base.model_copy(
                    update={"relationships": ("commerce.missing_relationship",)}
                ),
                PlanValidationCode.UNKNOWN_RELATIONSHIP,
            ),
        )
        for plan, expected_code in mutations:
            with self.subTest(code=expected_code):
                self.assertIn(expected_code, self._codes(plan))

    def test_disconnected_known_entities_are_rejected(self) -> None:
        plan = self._plan("commerce.geo_003")
        disconnected_spec = self.domain_pack.spec.model_copy(
            update={
                "relationships": tuple(
                    relationship
                    for relationship in self.domain_pack.spec.relationships
                    if relationship.name != "commerce.customer_geolocation"
                )
            }
        )
        disconnected_pack = self.domain_pack.model_copy(
            update={"spec": disconnected_spec}
        )

        self.assertIn(
            PlanValidationCode.UNREACHABLE_ENTITY,
            self._codes(plan, disconnected_pack),
        )

    def test_cross_fact_metric_requires_explicit_safe_grain_alignment(self) -> None:
        aligned = self._plan("commerce.metric_006")
        self.assertTrue(aligned.grain_alignment)
        self.assertTrue(self.validator.validate(aligned, self.domain_pack).valid)

        unsafe = aligned.model_copy(update={"grain_alignment": ()})
        codes = self._codes(unsafe)
        self.assertIn(PlanValidationCode.FANOUT_ALIGNMENT_REQUIRED, codes)
        self.assertIn(PlanValidationCode.METRIC_GRAIN_INCOMPATIBLE, codes)

    def test_metric_event_time_cannot_be_silently_replaced(self) -> None:
        plan = self._plan("commerce.metric_001")
        invalid_time = TimeRange(
            field="commerce.Order.purchased_at",
            start=plan.time_range.start,
            end=plan.time_range.end,
        )
        plan = plan.model_copy(update={"time_range": invalid_time})

        self.assertIn(
            PlanValidationCode.METRIC_TIME_FIELD_MISMATCH,
            self._codes(plan),
        )

    def test_ranking_order_detail_limit_result_shape_and_grain_are_enforced(self) -> None:
        ranking = self._plan("commerce.metric_002")
        detail = self._plan("commerce.detail_001")
        cases = (
            (
                ranking.model_copy(update={"ordering": ()}),
                PlanValidationCode.ORDERING_REQUIRED,
            ),
            (
                detail.model_copy(update={"limit": None}),
                PlanValidationCode.UNBOUNDED_DETAIL,
            ),
            (
                ranking.model_copy(update={"result_shape": "scalar"}),
                PlanValidationCode.RESULT_SHAPE_MISMATCH,
            ),
            (
                ranking.model_copy(update={"expected_grain": ()}),
                PlanValidationCode.EXPECTED_GRAIN_MISMATCH,
            ),
        )
        for plan, expected_code in cases:
            with self.subTest(code=expected_code):
                self.assertIn(expected_code, self._codes(plan))

    def test_raw_query_and_physical_configuration_fields_have_stable_codes(self) -> None:
        base = self._plan("commerce.metric_001").model_dump(mode="json")
        raw_key = "_".join(("raw", "sql"))
        query_text = " ".join(("sel" + "ect", "blocked"))
        raw_payload = {**base, raw_key: query_text}
        physical_key = "".join(("connec", "tor"))
        physical_payload = {**base, physical_key: "blocked"}

        raw_result = self.validator.validate(raw_payload, self.domain_pack)
        physical_result = self.validator.validate(physical_payload, self.domain_pack)

        self.assertEqual(
            raw_result.issues[0].code,
            PlanValidationCode.RAW_SQL_FORBIDDEN,
        )
        self.assertEqual(
            physical_result.issues[0].code,
            PlanValidationCode.PHYSICAL_IDENTIFIER_FORBIDDEN,
        )
        self.assertEqual(
            raw_result,
            self.validator.validate(raw_payload, self.domain_pack),
        )

    def test_natural_language_assumption_is_not_mistaken_for_raw_query_text(self) -> None:
        plan = self._plan("commerce.metric_001").model_copy(
            update={
                "assumptions": (
                    "result is derived from approved canonical semantics",
                )
            }
        )

        result = self.validator.validate(plan, self.domain_pack)

        self.assertTrue(result.valid, result.issues)

    def test_require_valid_raises_typed_error_with_stable_codes(self) -> None:
        invalid = self._plan("commerce.detail_001").model_copy(
            update={"limit": None}
        )

        with self.assertRaises(PlanValidationError) as raised:
            self.validator.require_valid(invalid, self.domain_pack)
        self.assertIn(
            PlanValidationCode.UNBOUNDED_DETAIL,
            raised.exception.codes,
        )

    def test_validator_revalidates_unchecked_model_copies_without_serializer_warnings(self) -> None:
        unchecked = self._plan("commerce.metric_002").model_copy(
            update={"result_shape": "scalar"}
        )

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            result = self.validator.validate(unchecked, self.domain_pack)

        self.assertIn(
            PlanValidationCode.RESULT_SHAPE_MISMATCH,
            tuple(issue.code for issue in result.issues),
        )
        self.assertEqual(captured, [])


if __name__ == "__main__":
    unittest.main()
