from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

from sqlglot import exp, parse_one


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
DOMAIN_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
ENTERPRISE_ROOT = PROJECT_ROOT / "packs" / "enterprises" / "olist"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.runtime import (
    load_bundle_manifest,
    load_domain_pack,
    load_enterprise_binding,
)
from data_agent.runtime.binding import BindingCompiler
from data_agent.runtime.models import PrincipalContext
from data_agent.skills import logical_plan_from_eval_case


class GrainAlignmentLoweringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.domain = load_domain_pack(DOMAIN_ROOT)
        cls.enterprise = load_enterprise_binding(ENTERPRISE_ROOT)
        cls.bundle = load_bundle_manifest(
            PROJECT_ROOT / "generated" / "bundles" / "olist-local.json",
            pack_lock=ENTERPRISE_ROOT / "pack.lock",
            schema_catalog=PROJECT_ROOT / "schema_catalog.json",
        )
        cls.compiler = BindingCompiler(cls.domain, cls.enterprise, cls.bundle)
        cls.plans = {
            case.id: logical_plan_from_eval_case(case, cls.domain)
            for case in cls.domain.spec.evals
        }
        cls.admin = PrincipalContext(
            tenant_id="platform",
            user_id="admin",
            roles=("admin",),
        )
        cls.seller = PrincipalContext(
            tenant_id="s1",
            user_id="seller-user",
            roles=("seller",),
        )

    def _compile(self, case_id: str, principal: PrincipalContext, *, low_having=False):
        plan = self.plans[case_id]
        if low_having and plan.having:
            plan = plan.model_copy(
                update={
                    "having": (
                        plan.having[0].model_copy(update={"value": 1}),
                        *plan.having[1:],
                    )
                }
            )
        bound = self.compiler.bind(plan, principal)
        return bound, self.compiler.compile(bound, principal)

    def test_alignment_proofs_and_ownership_guards_lower_to_non_expanding_ir(self) -> None:
        aligned_cases = (
            "commerce.metric_006",
            "commerce.metric_012",
            "commerce.join_005",
            "commerce.review_003",
            "commerce.tenant_004",
            "commerce.logistics_004",
            "commerce.followup_002",
            "commerce.geo_003",
        )
        for case_id in aligned_cases:
            principal = self.seller if case_id == "commerce.tenant_004" else self.admin
            with self.subTest(case=case_id):
                bound, prepared = self._compile(case_id, principal, low_having=True)
                self.assertEqual(
                    len(bound.alignment_proofs),
                    len(bound.logical_plan.grain_alignment),
                )
                statement = parse_one(prepared.executable_sql, read="postgres")
                if any(
                    item.strategy == "pre_aggregate"
                    for item in bound.alignment_proofs
                ):
                    self.assertIsNotNone(statement.find(exp.Subquery))
                    self.assertIsNotNone(statement.find(exp.Distinct))
                    self.assertGreaterEqual(
                        len(list(statement.find_all(exp.Group))),
                        1,
                    )
                else:
                    self.assertIsNotNone(statement.find(exp.Distinct))

        for case_id in ("commerce.payment_001", "commerce.detail_004"):
            with self.subTest(ownership_case=case_id):
                bound, prepared = self._compile(case_id, self.seller)
                self.assertEqual(
                    tuple(item.canonical_entity for item in bound.entities),
                    bound.logical_plan.entities,
                )
                self.assertTrue(bound.ownership_guards)
                statement = parse_one(prepared.executable_sql, read="postgres")
                self.assertIsNotNone(statement.find(exp.Exists))

    def test_duplicate_fixture_preserves_fact_and_detail_grains(self) -> None:
        connection = self._fixture_database()
        cases = (
            ("commerce.payment_001", self.seller, False, [("credit_card", 2, 105)]),
            ("commerce.detail_004", self.seller, False, [("o1", 0, "SP")]),
            ("commerce.metric_006", self.admin, False, [("credit_card", 128)]),
            (
                "commerce.metric_012",
                self.admin,
                False,
                [("B", "credit_card", 90), ("A", "credit_card", 38)],
            ),
            (
                "commerce.followup_002",
                self.admin,
                False,
                [("B", "credit_card", 90), ("A", "credit_card", 38)],
            ),
            ("commerce.review_003", self.seller, True, [("s1", 3, 2)]),
            (
                "commerce.join_005",
                self.admin,
                True,
                [("B", 1, 1), ("A", 3, 2)],
            ),
            (
                "commerce.logistics_004",
                self.admin,
                False,
                [("RJ", 2), ("SP", 3)],
            ),
            (
                "commerce.tenant_004",
                self.seller,
                False,
                [("2018-01-01", 1), ("2018-02-01", 5)],
            ),
        )
        for case_id, principal, low_having, expected in cases:
            with self.subTest(case=case_id):
                _, prepared = self._compile(
                    case_id,
                    principal,
                    low_having=low_having,
                )
                observed = self._execute_sqlite(connection, prepared)
                self.assertEqual(observed, expected)

    @staticmethod
    def _execute_sqlite(connection: sqlite3.Connection, prepared) -> list[tuple]:
        statement = parse_one(prepared.executable_sql, read="postgres")

        def normalize_postgres_functions(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Extract):
                return exp.Anonymous(
                    this="DATE_PART",
                    expressions=(node.this.copy(), node.expression.copy()),
                )
            return node

        def remove_casts(node: exp.Expression) -> exp.Expression:
            return node.this.copy() if isinstance(node, exp.Cast) else node

        sql = (
            statement.transform(normalize_postgres_functions)
            .transform(remove_casts)
            .sql(dialect="postgres")
        )
        # SQLite binds PostgreSQL-style ``$n`` placeholders by name.  A mapping
        # preserves the compiler-assigned parameter positions even when an outer
        # SELECT references a later parameter before an inner subquery does.
        values = {str(item.position): item.value for item in prepared.parameters}
        return [tuple(row) for row in connection.execute(sql, values).fetchall()]

    @staticmethod
    def _fixture_database() -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.execute("ATTACH DATABASE ':memory:' AS public")
        connection.create_function(
            "DATE_TRUNC",
            2,
            lambda unit, value: (
                f"{str(value)[:7]}-01"
                if str(unit).lower() == "month"
                else f"{str(value)[:4]}-01-01"
            ),
        )
        connection.create_function(
            "DATE_PART",
            2,
            lambda unit, value: value,
        )
        connection.executescript(
            """
            CREATE TABLE public.olist_orders_dataset (
              order_id TEXT, customer_id TEXT, order_status TEXT,
              order_purchase_timestamp NUMERIC, order_approved_at NUMERIC,
              order_delivered_carrier_date NUMERIC,
              order_delivered_customer_date NUMERIC,
              order_estimated_delivery_date NUMERIC
            );
            CREATE TABLE public.olist_order_items_dataset (
              order_id TEXT, order_item_id INTEGER, product_id TEXT, seller_id TEXT,
              shipping_limit_date TEXT, price NUMERIC, freight_value NUMERIC
            );
            CREATE TABLE public.olist_customers_dataset (
              customer_id TEXT, customer_unique_id TEXT, customer_city TEXT,
              customer_state TEXT, customer_zip_code_prefix TEXT
            );
            CREATE TABLE public.olist_sellers_dataset (
              seller_id TEXT, seller_city TEXT, seller_state TEXT,
              seller_zip_code_prefix TEXT
            );
            CREATE TABLE public.olist_products_dataset (
              product_id TEXT, product_category_name TEXT,
              product_name_lenght INTEGER, product_description_lenght INTEGER,
              product_photos_qty INTEGER, product_weight_g NUMERIC,
              product_length_cm NUMERIC, product_height_cm NUMERIC,
              product_width_cm NUMERIC
            );
            CREATE TABLE public.olist_order_payments_dataset (
              order_id TEXT, payment_sequential INTEGER, payment_type TEXT,
              payment_installments INTEGER, payment_value NUMERIC
            );
            CREATE TABLE public.olist_order_reviews_dataset (
              review_id TEXT, order_id TEXT, review_score INTEGER,
              review_comment_title TEXT, review_comment_message TEXT,
              review_creation_date TEXT, review_answer_timestamp TEXT
            );
            CREATE TABLE public.olist_geolocation_dataset (
              geolocation_row_id INTEGER, geolocation_zip_code_prefix TEXT,
              geolocation_lat NUMERIC, geolocation_lng NUMERIC,
              geolocation_city TEXT, geolocation_state TEXT
            );
            CREATE TABLE public.product_category_name_translation (
              product_category_name TEXT, product_category_name_english TEXT
            );

            INSERT INTO public.olist_orders_dataset VALUES
              ('o1','c1','canceled',0,NULL,172800,NULL,NULL),
              ('o2','c2','delivered',0,NULL,345600,NULL,NULL);
            INSERT INTO public.olist_order_items_dataset VALUES
              ('o1',1,'p1','s1','2018-05-01',10,1),
              ('o1',2,'p2','s1','2018-05-01',20,2),
              ('o1',3,'p3','s2','2018-05-01',90,0),
              ('o2',1,'p1','s1','2018-06-01',5,0);
            INSERT INTO public.olist_customers_dataset VALUES
              ('c1','person1','x','SP','1000'),
              ('c2','person2','y','SP','1000');
            INSERT INTO public.olist_sellers_dataset VALUES
              ('s1','x','SP','1000'),('s2','y','RJ','2000');
            INSERT INTO public.olist_products_dataset
              (product_id, product_category_name) VALUES
              ('p1','A'),('p2','A'),('p3','B');
            INSERT INTO public.olist_order_payments_dataset VALUES
              ('o1',1,'credit_card',1,60),
              ('o1',2,'credit_card',1,40),
              ('o2',1,'credit_card',1,5);
            INSERT INTO public.olist_order_reviews_dataset VALUES
              ('r1','o1',1,NULL,NULL,'2018-01-15',NULL),
              ('r2','o2',5,NULL,NULL,'2018-02-15',NULL);
            INSERT INTO public.olist_geolocation_dataset VALUES
              (1,'1000',1,2,'x','SP'),(2,'1000',3,4,'y','SP');
            """
        )
        return connection


if __name__ == "__main__":
    unittest.main()
