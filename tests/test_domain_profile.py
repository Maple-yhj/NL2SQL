import unittest
from pathlib import Path

from catalog.domain_loader import load_domain_profile
from catalog.domain_resolver import format_domain_context, resolve_domain_context
from engine.models import QueryIntent


class DomainProfileTests(unittest.TestCase):
    def test_loads_olist_profile_from_default_domain_catalog(self):
        profile = load_domain_profile("olist")

        self.assertEqual(profile.domain_id, "olist")
        self.assertIn("olist_order_items_dataset", profile.tables)
        self.assertIn("customer_state", profile.terms)
        self.assertEqual(profile.metrics["gmv"].base_table, "olist_order_items_dataset")

    def test_loads_profile_from_explicit_path(self):
        profile = load_domain_profile(Path("catalog/domains/olist.json"))

        self.assertEqual(profile.domain_id, "olist")
        self.assertIn("olist_order_items_dataset", profile.tables)

    def test_resolves_customer_state_gmv_tables_from_profile_terms(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="按客户州统计 2018 年 GMV",
            intent=QueryIntent(metrics=["gmv"], dimensions=["customer_state"]),
            metrics_result={
                "metrics": [
                    {
                        "metric_name": "gmv",
                        "base_table": "olist_order_items_dataset",
                        "join_tables": [],
                    }
                ]
            },
        )

        self.assertEqual(
            resolution.required_tables,
            [
                "olist_order_items_dataset",
                "olist_orders_dataset",
                "olist_customers_dataset",
            ],
        )
        self.assertIn("olist_customers_dataset.customer_state", resolution.required_columns)
        self.assertTrue(any("order_id" in hint for hint in resolution.join_hints))

    def test_resolves_payment_and_category_tables_together(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="按商品品类和支付方式交叉统计 GMV",
            intent=QueryIntent(metrics=["gmv"], dimensions=["product_category_name", "payment_type"]),
            metrics_result={
                "metrics": [
                    {
                        "metric_name": "gmv",
                        "base_table": "olist_order_items_dataset",
                        "join_tables": [],
                    }
                ]
            },
        )

        self.assertIn("olist_order_items_dataset", resolution.required_tables)
        self.assertIn("olist_products_dataset", resolution.required_tables)
        self.assertIn("olist_order_payments_dataset", resolution.required_tables)
        self.assertIn("product_category_name_translation", resolution.required_tables)

    def test_resolves_credit_card_installment_rule_with_required_filter(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="信用卡分期数越多，平均支付金额是否更高？按分期数统计平均支付金额",
            intent=QueryIntent(dimensions=["payment_installments"]),
            metrics_result={"metrics": []},
        )

        self.assertIn("olist_order_payments_dataset", resolution.required_tables)
        self.assertIn("olist_order_payments_dataset.payment_type", resolution.required_columns)
        self.assertIn("olist_order_payments_dataset.payment_installments", resolution.required_columns)
        self.assertIn("payment_type = 'credit_card'", resolution.required_filters)
        self.assertIn("credit_card_installments", resolution.matched_rules)

    def test_resolves_payment_detail_rule_with_default_columns(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="列出支付金额最高的 20 个订单支付记录",
            intent=QueryIntent(dimensions=["payment_value"]),
            metrics_result={"metrics": []},
        )

        self.assertIn("olist_order_payments_dataset", resolution.required_tables)
        self.assertEqual(
            resolution.default_columns,
            [
                "olist_order_payments_dataset.order_id",
                "olist_order_payments_dataset.payment_type",
                "olist_order_payments_dataset.payment_installments",
                "olist_order_payments_dataset.payment_sequential",
                "olist_order_payments_dataset.payment_value",
            ],
        )
        self.assertIn("payment_record_detail", resolution.matched_rules)
        self.assertIn(
            "Do not join olist_order_items_dataset for payment record detail ranking.",
            resolution.sql_hints,
        )

    def test_resolves_seller_count_location_rule_to_sellers_table(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="卖家数量最多的州和城市是哪里？",
            intent=QueryIntent(dimensions=["seller_state", "seller_city"]),
            metrics_result={"metrics": []},
        )

        self.assertEqual(resolution.required_tables, ["olist_sellers_dataset"])
        self.assertIn("olist_sellers_dataset.seller_state", resolution.required_columns)
        self.assertIn("olist_sellers_dataset.seller_city", resolution.required_columns)
        self.assertIn("COUNT(DISTINCT olist_sellers_dataset.seller_id)", resolution.sql_hints)

    def test_resolves_customer_count_location_rule_with_semantic_constraints(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="customers by state and city count",
            intent=QueryIntent(dimensions=["customer_state", "customer_city"]),
            metrics_result={"metrics": []},
        )

        self.assertIn("customer_count_by_location", resolution.matched_rules)
        self.assertEqual(resolution.required_tables, ["olist_customers_dataset"])
        self.assertIn("olist_customers_dataset.customer_state", resolution.required_columns)
        self.assertIn("olist_customers_dataset.customer_city", resolution.required_columns)
        self.assertIn("olist_customers_dataset.customer_id", resolution.required_columns)
        self.assertIn("customer_state", getattr(resolution, "required_group_by", []))
        self.assertIn("customer_city", getattr(resolution, "required_group_by", []))
        self.assertIn("customer_count DESC", getattr(resolution, "required_order_by", []))
        self.assertIn("COUNT(DISTINCT", getattr(resolution, "required_sql_fragments", []))
        self.assertIn("customer_id", getattr(resolution, "required_sql_fragments", []))
        self.assertIn("olist_order_items_dataset", getattr(resolution, "forbidden_tables", []))
        self.assertIn("olist_order_reviews_dataset", getattr(resolution, "forbidden_tables", []))

    def test_resolves_payment_amount_by_type_rule_with_sort_and_payment_only_scope(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="payment type order count and total payment amount",
            intent=QueryIntent(dimensions=["payment_type"]),
            metrics_result={"metrics": []},
        )

        self.assertIn("payment_amount_by_type", resolution.matched_rules)
        self.assertEqual(resolution.required_tables, ["olist_order_payments_dataset"])
        self.assertIn("olist_order_payments_dataset.order_id", resolution.required_columns)
        self.assertIn("olist_order_payments_dataset.payment_type", resolution.required_columns)
        self.assertIn("olist_order_payments_dataset.payment_value", resolution.required_columns)
        self.assertIn("payment_type", getattr(resolution, "required_group_by", []))
        self.assertIn("total_payment_amount DESC", getattr(resolution, "required_order_by", []))
        self.assertIn("SUM(payment_value)", getattr(resolution, "required_sql_fragments", []))
        self.assertIn("COUNT(DISTINCT", getattr(resolution, "required_sql_fragments", []))
        self.assertIn("olist_order_items_dataset", getattr(resolution, "forbidden_tables", []))

    def test_resolves_freight_exceeds_price_detail_rule_without_select_star(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="freight value greater than item price detail records",
            intent=QueryIntent(dimensions=["freight_value", "price"]),
            metrics_result={"metrics": []},
        )

        self.assertIn("freight_exceeds_price_detail", resolution.matched_rules)
        self.assertTrue(getattr(resolution, "forbid_select_star", False))
        self.assertEqual(
            resolution.default_columns,
            [
                "olist_order_items_dataset.order_id",
                "olist_order_items_dataset.product_id",
                "olist_order_items_dataset.seller_id",
                "olist_order_items_dataset.price",
                "olist_order_items_dataset.freight_value",
            ],
        )
        self.assertIn("freight_value > price", resolution.required_filters)

    def test_does_not_match_freight_price_detail_rule_for_plain_order_detail_columns(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="列出最近 20 条订单明细，包括订单号、卖家、商品、价格、运费和发货截止时间",
            intent=QueryIntent(dimensions=["price", "freight_value", "shipping_limit_date"]),
            metrics_result={"metrics": []},
        )

        self.assertNotIn("freight_exceeds_price_detail", resolution.matched_rules)
        self.assertFalse(resolution.forbid_select_star)

    def test_resolves_monthly_gmv_rule_with_date_trunc_month_alias(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="monthly GMV trend for 2018",
            intent=QueryIntent(metrics=["gmv"], dimensions=["month"]),
            metrics_result={
                "metrics": [
                    {
                        "metric_name": "gmv",
                        "base_table": "olist_order_items_dataset",
                        "join_tables": [],
                    }
                ]
            },
        )

        self.assertIn("monthly_gmv_trend", resolution.matched_rules)
        self.assertIn("month", getattr(resolution, "required_group_by", []))
        self.assertIn("date_trunc('month'", getattr(resolution, "required_sql_fragments", []))
        self.assertIn("shipping_limit_date", getattr(resolution, "required_sql_fragments", []))
        self.assertIn("AS month", getattr(resolution, "required_sql_fragments", []))
        self.assertIn("TO_CHAR(", getattr(resolution, "forbidden_sql_fragments", []))
        self.assertIn("EXTRACT(MONTH", getattr(resolution, "forbidden_sql_fragments", []))

    def test_resolves_monthly_gmv_growth_rule_without_month_ascending_order_constraint(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="monthly GMV fastest growth month",
            intent=QueryIntent(metrics=["gmv"], dimensions=["month"]),
            metrics_result={
                "metrics": [
                    {
                        "metric_name": "gmv",
                        "base_table": "olist_order_items_dataset",
                        "join_tables": [],
                    }
                ]
            },
        )

        self.assertIn("monthly_gmv_growth", resolution.matched_rules)
        self.assertNotIn("monthly_gmv_trend", resolution.matched_rules)
        self.assertEqual(getattr(resolution, "required_group_by", []), [])
        self.assertEqual(getattr(resolution, "required_order_by", []), [])
        self.assertIn("Order by the growth expression DESC and limit to 1.", resolution.sql_hints)

    def test_resolves_monthly_review_score_rule_with_review_date_trunc(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="monthly average review score trend",
            intent=QueryIntent(metrics=["avg_review_score"], dimensions=["month"]),
            metrics_result={
                "metrics": [
                    {
                        "metric_name": "avg_review_score",
                        "base_table": "olist_order_reviews_dataset",
                        "join_tables": [],
                    }
                ]
            },
        )

        self.assertIn("monthly_review_score", resolution.matched_rules)
        self.assertIn("month", getattr(resolution, "required_group_by", []))
        self.assertIn("date_trunc('month'", getattr(resolution, "required_sql_fragments", []))
        self.assertIn("review_creation_date", getattr(resolution, "required_sql_fragments", []))
        self.assertIn("TO_CHAR(", getattr(resolution, "forbidden_sql_fragments", []))

    def test_resolves_product_volume_rule_with_non_null_dimension_filters(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="查看体积最大的商品，返回商品 id、品类、长宽高和重量，取前 25 个",
            intent=QueryIntent(dimensions=["product_id"]),
            metrics_result={"metrics": []},
        )

        self.assertIn("product_volume_detail", resolution.matched_rules)
        self.assertIn("olist_products_dataset", resolution.required_tables)
        self.assertNotIn("product_category_name_translation", resolution.required_tables)
        self.assertNotIn("product_category_name_translation", resolution.forbidden_tables)
        self.assertIn("product_category_name_translation", resolution.optional_tables)
        self.assertEqual(resolution.required_filters, [])
        self.assertEqual(resolution.required_order_by, [])
        self.assertEqual(resolution.required_sql_fragments, [])
        self.assertEqual(resolution.default_columns, [])
        self.assertIn("Exclude products with NULL length, width, or height before ordering.", resolution.sql_hints)
        self.assertIn(
            "Do not join olist_order_items_dataset for product volume ranking.",
            resolution.sql_hints,
        )
        self.assertIn(
            "Select product_length_cm * product_width_cm * product_height_cm AS volume.",
            resolution.sql_hints,
        )

    def test_format_domain_context_includes_hard_rule_sections(self):
        profile = load_domain_profile("olist")
        resolution = resolve_domain_context(
            profile=profile,
            question="信用卡分期数越多，平均支付金额是否更高？按分期数统计平均支付金额",
            intent=QueryIntent(dimensions=["payment_installments"]),
            metrics_result={"metrics": []},
        )

        context = format_domain_context(resolution)

        self.assertIn("Hard domain rules:", context)
        self.assertIn("Required filters:", context)
        self.assertIn("payment_type = 'credit_card'", context)

    def test_format_domain_context_includes_semantic_constraint_sections(self):
        profile = load_domain_profile("olist")
        resolution = resolve_domain_context(
            profile=profile,
            question="monthly GMV trend for 2018",
            intent=QueryIntent(metrics=["gmv"], dimensions=["month"]),
            metrics_result={
                "metrics": [
                    {
                        "metric_name": "gmv",
                        "base_table": "olist_order_items_dataset",
                        "join_tables": [],
                    }
                ]
            },
        )

        context = format_domain_context(resolution)

        self.assertIn("Required GROUP BY:", context)
        self.assertIn("Required SQL fragments:", context)
        self.assertIn("Forbidden SQL fragments:", context)

    def test_format_domain_context_is_empty_when_nothing_matches(self):
        profile = load_domain_profile("olist")

        resolution = resolve_domain_context(
            profile=profile,
            question="show gmv",
            intent=QueryIntent(metrics=["gmv"]),
            metrics_result={
                "metrics": [
                    {"metric_name": "gmv", "base_table": "orders", "join_tables": []}
                ]
            },
        )

        self.assertEqual(resolution.required_tables, [])
        self.assertEqual(format_domain_context(resolution), "")


if __name__ == "__main__":
    unittest.main()
