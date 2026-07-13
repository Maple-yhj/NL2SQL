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
    PlanValidationCode,
    logical_plan_from_eval_case,
)


class ConnectivityAndGrainTests(unittest.TestCase):
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

    def test_every_declared_entity_must_be_connected_even_if_not_referenced(self) -> None:
        base = self._plan("commerce.metric_001")
        disconnected = base.model_copy(
            update={"entities": (*base.entities, "commerce.GeoLocation")}
        )

        self.assertIn(
            PlanValidationCode.UNREACHABLE_ENTITY,
            self._codes(disconnected),
        )

    def test_every_reference_source_requires_an_explicit_entity_declaration(self) -> None:
        cases = (
            (
                "metric_input",
                self._plan("commerce.metric_001").model_copy(update={"entities": ()}),
            ),
            (
                "dimension",
                self._plan("commerce.metric_002").model_copy(
                    update={"entities": ("commerce.OrderItem",)}
                ),
            ),
            (
                "filter",
                self._plan("commerce.followup_002").model_copy(
                    update={
                        "entities": (
                            "commerce.OrderItem",
                            "commerce.Product",
                            "commerce.Order",
                        )
                    }
                ),
            ),
            (
                "derived_input",
                self._plan("commerce.detail_006").model_copy(update={"entities": ()}),
            ),
        )

        for source, plan in cases:
            with self.subTest(source=source):
                self.assertIn(
                    PlanValidationCode.ENTITY_DECLARATION_REQUIRED,
                    self._codes(plan),
                )

    def test_order_item_detail_cannot_claim_the_coarser_order_grain(self) -> None:
        base = self._plan("commerce.detail_001")
        wrong_grain = base.model_copy(
            update={
                "entities": (*base.entities, "commerce.Order"),
                "relationships": (
                    *base.relationships,
                    "commerce.order_item_order",
                ),
                "expected_grain": ("commerce.Order.order_id",),
            }
        )

        self.assertIn(
            PlanValidationCode.EXPECTED_GRAIN_MISMATCH,
            self._codes(wrong_grain),
        )

    def test_detail_grain_is_anchored_at_the_finest_selected_row_entity(self) -> None:
        base = self._plan("commerce.detail_004")
        payment_detail = base.model_copy(
            update={
                "entities": (*base.entities, "commerce.Payment"),
                "relationships": (
                    *base.relationships,
                    "commerce.payment_order",
                ),
                "fields": (*base.fields, "commerce.Payment.amount"),
            }
        )

        self.assertIn(
            PlanValidationCode.EXPECTED_GRAIN_MISMATCH,
            self._codes(payment_detail),
        )

    def test_many_to_one_dimension_fields_preserve_detail_anchor_grain(self) -> None:
        plan = self._plan("commerce.detail_004")

        self.assertIn("commerce.Customer.state", plan.fields)
        self.assertEqual(plan.expected_grain, ("commerce.Order.order_id",))
        self.assertTrue(self.validator.validate(plan, self.domain_pack).valid)

    def test_duplicate_grain_alignment_is_rejected_explicitly(self) -> None:
        base = self._plan("commerce.metric_006")
        self.assertTrue(base.grain_alignment)
        duplicate = base.model_copy(
            update={
                "grain_alignment": (
                    base.grain_alignment[0],
                    base.grain_alignment[0],
                )
            }
        )

        self.assertIn(
            PlanValidationCode.DUPLICATE_REFERENCE,
            self._codes(duplicate),
        )

    def test_conflicting_grain_alignment_is_rejected_explicitly(self) -> None:
        base = self._plan("commerce.metric_006")
        original = base.grain_alignment[0]
        conflicting = original.model_copy(update={"strategy": "distinct"})
        plan = base.model_copy(
            update={"grain_alignment": (original, conflicting)}
        )

        self.assertIn(
            PlanValidationCode.UNSAFE_GRAIN_ALIGNMENT,
            self._codes(plan),
        )

    def test_every_dangerous_path_requires_its_own_alignment_proof(self) -> None:
        base = self._plan("commerce.review_003")
        self.assertGreater(len(base.grain_alignment), 1)
        incomplete = base.model_copy(
            update={"grain_alignment": base.grain_alignment[1:]}
        )

        self.assertIn(
            PlanValidationCode.FANOUT_ALIGNMENT_REQUIRED,
            self._codes(incomplete),
        )


if __name__ == "__main__":
    unittest.main()
