from __future__ import annotations

import importlib
import re
import shutil
import sys
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
PACK_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
sys.path.insert(0, str(SRC_ROOT))


class CommerceDomainPackContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loader = importlib.import_module("data_agent.runtime.profile_loader")

    def load_pack(self):
        return self.loader.load_domain_pack(PACK_ROOT)

    def test_split_domain_pack_loads_deterministically(self) -> None:
        expected_files = {
            "pack.yaml",
            "semantic-model.yaml",
            "metrics.yaml",
            "vocabulary.zh-CN.yaml",
            "policies.yaml",
            "evals.yaml",
        }
        self.assertEqual(
            {path.name for path in PACK_ROOT.glob("*.yaml")},
            expected_files,
        )

        first = self.load_pack()
        second = self.load_pack()

        self.assertEqual(first.metadata.name, "commerce")
        self.assertEqual(
            first.model_dump(mode="json", by_alias=True),
            second.model_dump(mode="json", by_alias=True),
        )

    def test_split_domain_pack_rejects_duplicate_yaml_keys(self) -> None:
        temporary_root = PROJECT_ROOT / "tests" / "contract" / ".commerce-pack-tmp"
        self.assertFalse(temporary_root.exists(), "previous pack temp remains")
        temporary_root.mkdir()
        self.addCleanup(shutil.rmtree, temporary_root)

        def duplicate_document(filename: str) -> None:
            path = pack_root / filename
            document = path.read_text(encoding="utf-8")
            path.write_text(f"{document}\n{document}", encoding="utf-8")

        def duplicate_nested_metric() -> None:
            path = pack_root / "metrics.yaml"
            document = path.read_text(encoding="utf-8")
            path.write_text(
                document.replace(
                    "    aggregation: sum\n",
                    "    aggregation: sum\n    aggregation: average\n",
                    1,
                ),
                encoding="utf-8",
            )

        def duplicate_nested_eval() -> None:
            path = pack_root / "evals.yaml"
            document = path.read_text(encoding="utf-8")
            path.write_text(
                document.replace(
                    "    question: 统计 2018 年每个月的 GMV 趋势\n",
                    "    question: 统计 2018 年每个月的 GMV 趋势\n"
                    "    question: 重复问题\n",
                    1,
                ),
                encoding="utf-8",
            )

        mutations = {
            "metadata": lambda: duplicate_document("pack.yaml"),
            "fragment_section": lambda: duplicate_document("metrics.yaml"),
            "nested_metric": duplicate_nested_metric,
            "nested_eval": duplicate_nested_eval,
        }
        for name, mutate in mutations.items():
            with self.subTest(location=name):
                pack_root = temporary_root / name / "commerce"
                shutil.copytree(PACK_ROOT, pack_root)
                mutate()
                with self.assertRaises(self.loader.PackLoadError):
                    self.loader.load_domain_pack(pack_root)

    def test_runtime_package_reexports_domain_pack_loader(self) -> None:
        runtime = importlib.import_module("data_agent.runtime")
        self.assertIs(runtime.load_domain_pack, self.loader.load_domain_pack)

    def test_pack_defines_nine_entities_and_eight_relationships(self) -> None:
        pack = self.load_pack()
        self.assertEqual(
            set(pack.spec.entities),
            {
                "commerce.Order",
                "commerce.OrderItem",
                "commerce.Customer",
                "commerce.Seller",
                "commerce.Product",
                "commerce.Payment",
                "commerce.Review",
                "commerce.GeoLocation",
                "commerce.CategoryTranslation",
            },
        )
        self.assertEqual(len(pack.spec.relationships), 8)
        self.assertEqual(
            {relationship.name for relationship in pack.spec.relationships},
            {
                "commerce.order_item_order",
                "commerce.order_customer",
                "commerce.order_item_product",
                "commerce.order_item_seller",
                "commerce.payment_order",
                "commerce.review_order",
                "commerce.product_category_translation",
                "commerce.customer_geolocation",
            },
        )

    def test_pack_defines_the_four_approved_metric_semantics(self) -> None:
        pack = self.load_pack()
        metrics = pack.spec.metrics
        self.assertEqual(
            set(metrics),
            {
                "commerce.gmv",
                "commerce.order_count",
                "commerce.average_item_price",
                "commerce.average_review_score",
            },
        )

        gmv = metrics["commerce.gmv"]
        self.assertEqual(gmv.aggregation, "sum")
        self.assertEqual(gmv.combine, "add")
        self.assertEqual(
            gmv.inputs,
            (
                "commerce.OrderItem.item_price",
                "commerce.OrderItem.freight_amount",
            ),
        )
        self.assertEqual(
            gmv.event_time,
            "commerce.OrderItem.shipping_limit_at",
        )

        order_count = metrics["commerce.order_count"]
        self.assertEqual(order_count.aggregation, "count_distinct")
        self.assertEqual(
            order_count.inputs,
            ("commerce.OrderItem.order_id",),
        )

        average_item_price = metrics["commerce.average_item_price"]
        self.assertEqual(average_item_price.aggregation, "average")
        self.assertEqual(
            average_item_price.inputs,
            ("commerce.OrderItem.item_price",),
        )

        average_review_score = metrics["commerce.average_review_score"]
        self.assertEqual(average_review_score.aggregation, "average")
        self.assertEqual(
            average_review_score.inputs,
            ("commerce.Review.score",),
        )

    def test_pack_migrates_exactly_48_logical_evaluations(self) -> None:
        pack = self.load_pack()
        cases = pack.spec.evals
        case_ids = [case.id for case in cases]

        self.assertEqual(len(cases), 48)
        self.assertEqual(len(set(case_ids)), 48)
        self.assertTrue(all(case_id.startswith("commerce.") for case_id in case_ids))

        raw_evals = yaml.safe_load(
            (PACK_ROOT / "evals.yaml").read_text(encoding="utf-8")
        )["evals"]
        allowed_keys = {
            "id",
            "question",
            "analysisType",
            "expectedMetrics",
            "expectedEntities",
            "expectedDimensions",
            "expectedFields",
            "filters",
            "time",
            "calculations",
            "having",
            "ordering",
            "limit",
            "expectedGrain",
            "context",
        }
        required_keys = {
            "id",
            "question",
            "analysisType",
            "expectedMetrics",
            "expectedEntities",
            "expectedDimensions",
            "expectedFields",
            "expectedGrain",
            "context",
        }
        for raw_case, case in zip(raw_evals, cases, strict=True):
            with self.subTest(case=case.id):
                self.assertLessEqual(set(raw_case), allowed_keys)
                self.assertLessEqual(required_keys, set(raw_case))
                self.assertTrue(
                    all(value.startswith("commerce.") for value in case.expected_metrics)
                )
                self.assertTrue(
                    all(value.startswith("commerce.") for value in case.expected_entities)
                )
                self.assertTrue(case.expected_entities)
                self.assertTrue(
                    case.expected_metrics
                    or case.expected_fields
                    or case.calculations
                )

    def test_logical_evaluations_preserve_key_query_oracles(self) -> None:
        cases = {case.id: case for case in self.load_pack().spec.evals}

        monthly_gmv = cases["commerce.metric_001"]
        self.assertEqual(monthly_gmv.analysis_type, "trend")
        self.assertEqual(
            monthly_gmv.expected_dimensions,
            ("commerce.OrderItem.shipping_limit_at",),
        )
        self.assertEqual(monthly_gmv.time.grain, "month")
        self.assertEqual(monthly_gmv.time.start, "2018-01-01")
        self.assertEqual(monthly_gmv.time.end, "2019-01-01")
        self.assertEqual(monthly_gmv.ordering[0].direction, "asc")

        low_reviews = cases["commerce.join_005"]
        self.assertEqual(
            low_reviews.expected_dimensions,
            ("commerce.Product.category_code",),
        )
        self.assertEqual(low_reviews.calculations[0].id, "commerce.review_count")
        self.assertEqual(low_reviews.having[0].ref, "commerce.review_count")
        self.assertEqual(low_reviews.having[0].operator, "gte")
        self.assertEqual(low_reviews.having[0].value, 100)
        self.assertEqual(low_reviews.ordering[0].direction, "asc")
        self.assertEqual(low_reviews.limit, 10)

        recent_items = cases["commerce.detail_001"]
        self.assertEqual(recent_items.analysis_type, "detail")
        self.assertEqual(len(recent_items.expected_fields), 6)
        self.assertEqual(
            recent_items.ordering[0].ref,
            "commerce.OrderItem.shipping_limit_at",
        )
        self.assertEqual(recent_items.ordering[0].direction, "desc")
        self.assertEqual(recent_items.limit, 20)

        tenant_gmv = cases["commerce.tenant_001"]
        self.assertEqual(tenant_gmv.context.tenant_scope, "seller")
        self.assertEqual(
            tenant_gmv.filters[0].ref,
            "commerce.OrderItem.seller_id",
        )

        follow_up = cases["commerce.followup_001"]
        self.assertEqual(follow_up.context.mode, "follow_up")
        self.assertIn("metrics", follow_up.context.preserve)
        self.assertIn("time_range", follow_up.context.preserve)
        self.assertEqual(
            follow_up.expected_grain,
            (
                "commerce.OrderItem.shipping_limit_at",
                "commerce.Customer.state",
            ),
        )

    def test_domain_pack_files_recursively_exclude_olist_physical_details(self) -> None:
        forbidden_keys = {
            "relation",
            "table",
            "column",
            "schema",
            "sql",
            "connector",
            "connectionref",
        }
        forbidden_tokens = {
            "olist",
            "product_category_name_translation",
            "freight_value",
            "shipping_limit_date",
            "order_item_id",
            "order_status",
            "order_purchase_timestamp",
            "order_approved_at",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
            "customer_unique_id",
            "customer_zip_code_prefix",
            "customer_city",
            "customer_state",
            "seller_zip_code_prefix",
            "seller_city",
            "seller_state",
            "product_category_name",
            "product_name_lenght",
            "product_description_lenght",
            "product_photos_qty",
            "product_weight_g",
            "product_length_cm",
            "product_height_cm",
            "product_width_cm",
            "payment_sequential",
            "payment_type",
            "payment_installments",
            "payment_value",
            "review_score",
            "review_comment_title",
            "review_comment_message",
            "review_creation_date",
            "review_answer_timestamp",
            "geolocation_zip_code_prefix",
            "geolocation_lat",
            "geolocation_lng",
            "geolocation_city",
            "geolocation_state",
        }

        def normalize_key(value: object) -> str:
            return re.sub(r"[^a-z0-9]", "", str(value).lower())

        def walk(value: object, path: str) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    self.assertNotIn(normalize_key(key), forbidden_keys, path)
                    walk(nested, f"{path}.{key}")
            elif isinstance(value, list):
                for index, nested in enumerate(value):
                    walk(nested, f"{path}[{index}]")
            elif isinstance(value, str):
                lowered = value.lower()
                for token in forbidden_tokens:
                    if token == "olist":
                        self.assertNotIn(token, lowered, f"{path}: {token}")
                        continue
                    self.assertIsNone(
                        re.search(
                            rf"(?<![a-z0-9_]){re.escape(token)}(?![a-z0-9_])",
                            lowered,
                        ),
                        f"{path}: {token}",
                    )
                self.assertIsNone(
                    re.search(
                        r"\b(select|from|join|where|group\s+by|order\s+by)\b",
                        lowered,
                    ),
                    path,
                )

        for path in sorted(PACK_ROOT.rglob("*")):
            if not path.is_file():
                continue
            with self.subTest(path=path.relative_to(PACK_ROOT)):
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                walk(document, path.name)


if __name__ == "__main__":
    unittest.main()
