from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

from pydantic import BaseModel, ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
PACK_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.runtime import load_domain_pack
from data_agent.skills import (
    CommercePlanValidator,
    LogicalFilter,
    PlanValidationCode,
    logical_plan_from_eval_case,
)


class StructuralIntegrityAndContentSecurityTests(unittest.TestCase):
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

    def test_validator_sees_top_level_forbidden_fields_hidden_by_unchecked_copy(self) -> None:
        base = self._plan("commerce.metric_001")
        query_key = "_".join(("raw", "sql"))
        connector_key = "".join(("connec", "tor"))
        query_text = " ".join(("sel" + "ect", "1"))
        hidden = BaseModel.model_copy(
            base,
            update={query_key: query_text, connector_key: "postgres"},
        )

        self.assertTrue(hasattr(hidden, query_key))
        self.assertNotIn(query_key, hidden.model_dump(mode="json"))
        codes = self._codes(hidden)
        self.assertIn(PlanValidationCode.RAW_SQL_FORBIDDEN, codes)
        self.assertIn(PlanValidationCode.PHYSICAL_IDENTIFIER_FORBIDDEN, codes)

    def test_validator_recurses_into_nested_models_before_serialization(self) -> None:
        base = self._plan("commerce.detail_005")
        connector_key = "".join(("connec", "tor"))
        hidden_filter = BaseModel.model_copy(
            base.filters[0],
            update={connector_key: "postgres"},
        )
        hidden_plan = BaseModel.model_copy(
            base,
            update={"filters": (hidden_filter, *base.filters[1:])},
        )

        self.assertIn(
            PlanValidationCode.PHYSICAL_IDENTIFIER_FORBIDDEN,
            self._codes(hidden_plan),
        )

    def test_unchecked_unknown_non_security_field_is_an_invalid_contract(self) -> None:
        base = self._plan("commerce.metric_001")
        hidden = BaseModel.model_copy(base, update={"mystery": "blocked"})

        self.assertIn(
            PlanValidationCode.INVALID_PLAN_CONTRACT,
            self._codes(hidden),
        )

    def test_normal_model_copy_rejects_unknown_update_fields(self) -> None:
        base = self._plan("commerce.metric_001")

        with self.assertRaises((ValidationError, ValueError)):
            base.model_copy(update={"mystery": "blocked"})

    def test_qualified_relation_is_rejected_in_filter_assumption_and_evidence(self) -> None:
        base = self._plan("commerce.detail_004")
        qualified_relation = ".".join(("public", "olist_order_items"))
        physical_filter = base.filters[0].model_copy(
            update={"value": qualified_relation}
        )
        filter_plan = base.model_copy(update={"filters": (physical_filter,)})
        assumption_plan = base.model_copy(
            update={"assumptions": (f"请读取 {qualified_relation}",)}
        )
        evidence_payload = base.model_dump(mode="json", by_alias=True)
        evidence_payload["requestedEvidence"] = [qualified_relation]

        for name, candidate in (
            ("filter", filter_plan),
            ("assumption", assumption_plan),
            ("evidence", evidence_payload),
        ):
            with self.subTest(location=name):
                result = self.validator.validate(candidate, self.domain_pack)
                self.assertFalse(result.valid)
                self.assertIn(
                    PlanValidationCode.PHYSICAL_IDENTIFIER_FORBIDDEN,
                    tuple(issue.code for issue in result.issues),
                )

    def test_credential_and_raw_query_markers_are_rejected_recursively(self) -> None:
        base = self._plan("commerce.metric_001")
        credential = "=".join(("password", "blocked"))
        raw_query = " ".join(
            ("sel" + "ect", "amount", "fr" + "om", "blocked")
        )
        credential_plan = base.model_copy(
            update={"assumptions": (credential,)}
        )
        query_plan = base.model_copy(update={"assumptions": (raw_query,)})

        self.assertIn(
            PlanValidationCode.PHYSICAL_IDENTIFIER_FORBIDDEN,
            self._codes(credential_plan),
        )
        self.assertIn(
            PlanValidationCode.RAW_SQL_FORBIDDEN,
            self._codes(query_plan),
        )

    def test_physical_column_style_literal_is_rejected_without_a_qualified_relation(self) -> None:
        base = self._plan("commerce.detail_004")
        physical_column = "_".join(("order", "purchase", "timestamp"))
        predicate = base.filters[0].model_copy(update={"value": physical_column})
        plan = base.model_copy(update={"filters": (predicate,)})

        self.assertIn(
            PlanValidationCode.PHYSICAL_IDENTIFIER_FORBIDDEN,
            self._codes(plan),
        )

    def test_ordinary_chinese_business_text_and_context_reference_remain_valid(self) -> None:
        base = self._plan("commerce.tenant_001")
        plan = base.model_copy(
            update={"assumptions": ("按业务口径统计已完成订单",)}
        )

        self.assertEqual(plan.filters[0].value, "context.seller_id")
        self.assertTrue(
            self.validator.validate(plan, self.domain_pack).valid
        )

    def test_non_finite_filter_values_are_rejected_by_models_and_validator(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    LogicalFilter(
                        ref="commerce.OrderItem.item_price",
                        operator="gt",
                        value=value,
                    )

        base = self._plan("commerce.detail_002")
        hidden_filter = BaseModel.model_copy(
            base.filters[0] if base.filters else LogicalFilter(
                ref="commerce.OrderItem.item_price",
                operator="gt",
                value=0,
            ),
            update={"value": float("nan")},
        )
        hidden = BaseModel.model_copy(base, update={"filters": (hidden_filter,)})
        self.assertIn(
            PlanValidationCode.NON_FINITE_NUMBER,
            self._codes(hidden),
        )

    def test_canonical_json_is_standard_json_and_never_emits_nan(self) -> None:
        base = self._plan("commerce.detail_002")
        canonical = base.canonical_json()

        self.assertEqual(json.loads(canonical), json.loads(canonical))
        self.assertNotIn("NaN", canonical)
        hidden_filter = BaseModel.model_copy(
            LogicalFilter(
                ref="commerce.OrderItem.item_price",
                operator="gt",
                value=0,
            ),
            update={"value": math.nan},
        )
        hidden = BaseModel.model_copy(base, update={"filters": (hidden_filter,)})
        with self.assertRaises(ValueError):
            hidden.canonical_json()


if __name__ == "__main__":
    unittest.main()
