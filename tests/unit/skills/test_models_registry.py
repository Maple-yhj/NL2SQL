from __future__ import annotations

import sys
import unittest
from pathlib import Path

from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
PACK_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.runtime import load_domain_pack
from data_agent.skills import (
    ALLOWED_TOOL_CAPABILITIES,
    DerivedCalculation,
    LogicalFilter,
    LogicalOrdering,
    LogicalQueryPlan,
    PlanContext,
    ResultShape,
    SkillInput,
    SkillManifest,
    SkillRegistry,
    TimeRange,
    WindowSpec,
)


class SkillManifestRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.domain_pack = load_domain_pack(PACK_ROOT)

    def test_builtin_registry_contains_exactly_one_versioned_commerce_skill(self) -> None:
        registry = SkillRegistry.builtin()

        self.assertEqual(registry.keys(), ("commerce.analytics@1.0.0",))
        self.assertEqual(len(registry.skills()), 1)
        self.assertIs(
            registry.get("commerce.analytics"),
            registry.get("commerce.analytics", version="1.0.0"),
        )
        self.assertIsNone(registry.get("commerce.analytics", version="2.0.0"))
        self.assertIsNone(registry.get("commerce.unknown"))
        self.assertFalse(hasattr(registry, "register"))
        self.assertFalse(hasattr(registry, "discover"))
        self.assertFalse(hasattr(registry, "load_plugin"))

    def test_manifest_is_complete_frozen_and_declares_only_six_allowed_tools(self) -> None:
        manifest = SkillRegistry.builtin().get("commerce.analytics").manifest

        self.assertIsInstance(manifest, SkillManifest)
        self.assertEqual(manifest.skill_id, "commerce.analytics")
        self.assertEqual(manifest.version, "1.0.0")
        self.assertEqual(manifest.domain, "commerce")
        self.assertEqual(manifest.allowed_tools, ALLOWED_TOOL_CAPABILITIES)
        self.assertEqual(
            manifest.allowed_tools,
            (
                "semantic.search",
                "data.inspect",
                "query.compile",
                "query.execute",
                "result.profile",
                "answer.render",
            ),
        )
        self.assertEqual(
            manifest.required_tool_capabilities,
            ALLOWED_TOOL_CAPABILITIES,
        )
        self.assertTrue(manifest.intent_signatures)
        self.assertTrue(manifest.required_semantic_ids)
        self.assertTrue(manifest.graph_fragment)
        self.assertEqual(
            manifest.logical_plan_schema,
            "dataagent.io/skills/logical-query-plan/v1",
        )
        self.assertTrue(manifest.validators)
        self.assertEqual(
            manifest.output_schema,
            "dataagent.io/skills/plan-validation-result/v1",
        )
        self.assertEqual(manifest.memory_write_policy, "proposal_only")
        self.assertEqual(manifest.eval_suite_ref, "commerce.evals@1.0.0")

        with self.assertRaises(ValidationError):
            manifest.version = "2.0.0"

    def test_skill_input_is_typed_frozen_and_domain_pack_backed(self) -> None:
        skill_input = SkillInput(
            question="2018 年每月成交总额",
            contextualized_question="统计 2018 年每月成交总额",
            commerce_semantic_snapshot=self.domain_pack,
            accessible_semantic_resources=[
                "commerce.gmv",
                "commerce.OrderItem.shipping_limit_at",
            ],
            approved_memories=["用户偏好使用月粒度"],
            conversation_summary="用户正在分析年度趋势",
        )

        self.assertIs(skill_input.commerce_semantic_snapshot, self.domain_pack)
        self.assertEqual(
            skill_input.accessible_semantic_resources,
            (
                "commerce.gmv",
                "commerce.OrderItem.shipping_limit_at",
            ),
        )
        self.assertEqual(skill_input.approved_memories, ("用户偏好使用月粒度",))
        with self.assertRaises(ValidationError):
            skill_input.question = "changed"


class LogicalQueryPlanModelTests(unittest.TestCase):
    def _comparison_plan(self) -> LogicalQueryPlan:
        return LogicalQueryPlan(
            analysis_type="comparison",
            metrics=["commerce.gmv"],
            entities=["commerce.OrderItem", "commerce.Seller"],
            relationships=["commerce.order_item_seller"],
            dimensions=[
                "commerce.Seller.state",
                "commerce.OrderItem.shipping_limit_at",
            ],
            fields=[],
            filters=[
                LogicalFilter(
                    ref="commerce.Seller.state",
                    operator="in",
                    value=["SP", "RJ"],
                )
            ],
            time_range=TimeRange(
                field="commerce.OrderItem.shipping_limit_at",
                start="2017-01-01",
                end="2019-01-01",
            ),
            time_grain="year",
            ordering=[
                LogicalOrdering(ref="commerce.Seller.state", direction="asc"),
                LogicalOrdering(
                    ref="commerce.OrderItem.shipping_limit_at",
                    direction="asc",
                ),
            ],
            limit=10,
            expected_grain=[
                "commerce.Seller.state",
                "commerce.OrderItem.shipping_limit_at",
            ],
            assumptions=["时间范围使用半开区间"],
            requested_evidence=["semantic_resolution", "query_result"],
            derived_calculations=[
                DerivedCalculation(
                    id="commerce.yearly_gmv_change",
                    operation="growth",
                    inputs=["commerce.gmv"],
                    partition_by=["commerce.Seller.state"],
                )
            ],
            having=[],
            window_specs=[
                WindowSpec(
                    id="commerce.yearly_gmv_change_window",
                    calculation="commerce.yearly_gmv_change",
                    axis_ref="commerce.OrderItem.shipping_limit_at",
                    partition_by=["commerce.Seller.state"],
                    ordering=[
                        LogicalOrdering(
                            ref="commerce.OrderItem.shipping_limit_at",
                            direction="asc",
                        )
                    ],
                    output_grain=[
                        "commerce.Seller.state",
                        "commerce.OrderItem.shipping_limit_at",
                    ],
                )
            ],
            result_shape="table",
            context=PlanContext(),
        )

    def test_plan_models_cover_filters_time_windows_and_derived_calculations(self) -> None:
        plan = self._comparison_plan()

        self.assertEqual(plan.analysis_type, "comparison")
        self.assertEqual(plan.result_shape, ResultShape.TABLE)
        self.assertEqual(plan.filters[0].value, ("SP", "RJ"))
        self.assertEqual(plan.time_grain, "year")
        self.assertEqual(plan.derived_calculations[0].operation, "growth")
        self.assertEqual(
            plan.window_specs[0].calculation,
            "commerce.yearly_gmv_change",
        )
        self.assertEqual(
            plan.window_specs[0].output_grain,
            plan.expected_grain,
        )

    def test_plan_hash_and_canonical_json_are_stable_for_equivalent_inputs(self) -> None:
        first = self._comparison_plan()
        second = LogicalQueryPlan.model_validate(
            first.model_dump(mode="json", by_alias=True)
        )

        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.stable_hash(), second.stable_hash())
        self.assertEqual(len(first.stable_hash()), 64)

    def test_predicate_values_are_operator_typed(self) -> None:
        with self.assertRaises(ValidationError):
            LogicalFilter(ref="commerce.Order.status", operator="eq")
        with self.assertRaises(ValidationError):
            LogicalFilter(
                ref="commerce.Order.status",
                operator="is_null",
                value="canceled",
            )
        with self.assertRaises(ValidationError):
            LogicalFilter(
                ref="commerce.Order.status",
                operator="in",
                value="canceled",
            )

    def test_plan_contract_rejects_untyped_extra_fields(self) -> None:
        payload = self._comparison_plan().model_dump(mode="json")
        payload["unexpected"] = "blocked"
        with self.assertRaises(ValidationError):
            LogicalQueryPlan.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
