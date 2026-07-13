from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

from sqlglot import exp, parse


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
DOMAIN_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
ENTERPRISE_ROOT = PROJECT_ROOT / "packs" / "enterprises" / "olist"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.runtime import (
    load_bundle_manifest,
    load_domain_pack,
    load_enterprise_binding,
    stable_digest,
)
from data_agent.runtime.binding import (
    BindingCompiler,
    BindingError,
    BindingErrorCode,
    BoundQueryPlan,
    PreparedQuery,
)
from data_agent.runtime.composition import ResolvedRuntimeBundle
from data_agent.runtime.models import PrincipalContext
from data_agent.skills import logical_plan_from_eval_case


class BindingCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.domain = load_domain_pack(DOMAIN_ROOT)
        cls.enterprise = load_enterprise_binding(ENTERPRISE_ROOT)
        cls.bundle = load_bundle_manifest(
            PROJECT_ROOT / "generated" / "bundles" / "olist-local.json",
            pack_lock=ENTERPRISE_ROOT / "pack.lock",
            schema_catalog=PROJECT_ROOT / "schema_catalog.json",
        )
        cls.plans = {
            case.id: logical_plan_from_eval_case(case, cls.domain)
            for case in cls.domain.spec.evals
        }
        cls.admin = PrincipalContext(
            tenant_id="platform",
            user_id="admin-user",
            roles=("admin",),
        )
        cls.seller = PrincipalContext(
            tenant_id="seller-tenant-42",
            user_id="seller-user",
            roles=("seller",),
        )
        cls.compiler = BindingCompiler(cls.domain, cls.enterprise, cls.bundle)

    def test_all_48_plans_bind_and_compile_offline_for_admin_and_seller(self) -> None:
        self.assertEqual(len(self.plans), 48)
        allowed = set(self.enterprise.spec.policies.relation_allowlist)

        for case_id, plan in self.plans.items():
            for principal in (self.admin, self.seller):
                with self.subTest(case=case_id, roles=principal.roles):
                    bound = self.compiler.bind(plan, principal)
                    prepared = self.compiler.compile(bound, principal)

                    self.assertIsInstance(bound, BoundQueryPlan)
                    self.assertIsInstance(prepared, PreparedQuery)
                    self.assertEqual(bound.logical_plan_hash, plan.stable_hash())
                    self.assertEqual(prepared.logical_plan_hash, plan.stable_hash())
                    self.assertTrue(bound.selected_columns)
                    self.assertTrue(set(bound.physical_relations).issubset(allowed))
                    self.assertEqual(
                        set(prepared.allowed_relations),
                        set(bound.physical_relations),
                    )
                    statements = parse(prepared.executable_sql, read="postgres")
                    self.assertEqual(len(statements), 1)
                    statement = statements[0]
                    self.assertIsInstance(statement, exp.Select)
                    self.assertFalse(
                        any(
                            statement.find(node_type) is not None
                            for node_type in (
                                exp.Insert,
                                exp.Update,
                                exp.Delete,
                                exp.Create,
                                exp.Drop,
                                exp.Alter,
                                exp.Command,
                            )
                        )
                    )
                    positions = tuple(item.position for item in prepared.parameters)
                    self.assertEqual(positions, tuple(range(1, len(positions) + 1)))
                    for position in positions:
                        self.assertIn(f"${position}", prepared.executable_sql)
                    self.assertNotRegex(
                        prepared.executable_sql,
                        r"(?i)\b(?:insert|update|delete|drop|alter|create|copy)\b",
                    )
                    self.assertNotIn("{{", prepared.executable_sql)
                    self.assertNotIn("{%", prepared.executable_sql)

    def test_compilation_is_byte_deterministic_and_parameterizes_values_and_limit(self) -> None:
        plan = self.plans["commerce.metric_002"]

        first = self.compiler.compile(self.compiler.bind(plan, self.seller), self.seller)
        second = self.compiler.compile(self.compiler.bind(plan, self.seller), self.seller)

        self.assertEqual(first, second)
        self.assertEqual(first.executable_sql, second.executable_sql)
        self.assertNotIn("seller-tenant-42", first.executable_sql)
        self.assertNotIn("2017-01-01", first.executable_sql)
        self.assertNotIn("2018-01-01", first.executable_sql)
        self.assertNotRegex(first.executable_sql, r"(?i)\bLIMIT\s+10\b")
        self.assertIn("seller-tenant-42", [item.value for item in first.parameters])
        self.assertIn("2017-01-01", [item.value for item in first.parameters])
        self.assertIn("2018-01-01", [item.value for item in first.parameters])
        self.assertIn(10, [item.value for item in first.parameters])
        self.assertIn("tenant_scope", [item.purpose for item in first.parameters])
        self.assertIn("limit", [item.purpose for item in first.parameters])
        self.assertEqual(first.max_rows, 10)

    def test_non_admin_always_gets_structural_seller_scope_and_admin_can_bypass(self) -> None:
        global_plan = self.plans["commerce.metric_001"]

        seller_bound = self.compiler.bind(global_plan, self.seller)
        seller_query = self.compiler.compile(seller_bound, self.seller)
        admin_bound = self.compiler.bind(global_plan, self.admin)
        admin_query = self.compiler.compile(admin_bound, self.admin)

        policy_predicates = [
            item for item in seller_bound.predicates if item.policy_enforced
        ]
        self.assertFalse(policy_predicates)
        self.assertEqual(len(seller_bound.ownership_guards), 1)
        self.assertEqual(
            seller_bound.ownership_guards[0].terminal_scope_ref,
            "commerce.Seller.seller_id",
        )
        self.assertIn(
            "public.olist_sellers_dataset",
            seller_bound.physical_relations,
        )
        self.assertIn("seller-tenant-42", [p.value for p in seller_query.parameters])
        self.assertIsNotNone(parse(seller_query.executable_sql, read="postgres")[0].find(exp.Exists))
        self.assertFalse(admin_bound.required_access.tenant_scoped)
        self.assertTrue(admin_bound.required_access.admin_bypass)
        self.assertFalse(any(item.policy_enforced for item in admin_bound.predicates))
        self.assertFalse(admin_bound.ownership_guards)
        self.assertNotIn("seller-tenant-42", [p.value for p in admin_query.parameters])
        self.assertNotEqual(seller_query.policy_decision_id, admin_query.policy_decision_id)

    def test_only_roles_claim_can_trigger_admin_bypass(self) -> None:
        plan = self.plans["commerce.metric_001"]
        tenant_named_admin = PrincipalContext(
            tenant_id="admin",
            user_id="seller-user",
            roles=("seller",),
        )

        bound = self.compiler.bind(plan, tenant_named_admin)

        self.assertTrue(bound.required_access.tenant_scoped)
        self.assertFalse(bound.required_access.admin_bypass)
        self.assertTrue(
            any(item.policy_enforced for item in bound.predicates)
            or bool(bound.ownership_guards)
        )

    def test_bundle_is_the_exact_authority_and_tampering_fails_closed(self) -> None:
        raw = self.bundle.model_dump(mode="python")
        raw["physical_bindings"]["commerce.Order"]["relation"] = "public.evil"
        payload = {key: value for key, value in raw.items() if key != "digest"}
        raw["digest"] = stable_digest(payload)
        tampered = ResolvedRuntimeBundle.model_validate(raw)

        with self.assertRaises(BindingError) as raised:
            BindingCompiler(self.domain, self.enterprise, tampered)

        self.assertEqual(raised.exception.code, BindingErrorCode.BUNDLE_MISMATCH)

    def test_bound_and_prepared_contracts_are_frozen(self) -> None:
        plan = self.plans["commerce.metric_001"]
        bound = self.compiler.bind(plan, self.admin)
        prepared = self.compiler.compile(bound, self.admin)

        with self.assertRaises(Exception):
            bound.limit = 7  # type: ignore[misc]
        with self.assertRaises(Exception):
            prepared.executable_sql = "SELECT 1"  # type: ignore[misc]

    def test_sql_uses_only_quoted_authorized_physical_relations(self) -> None:
        allowed = set(self.enterprise.spec.policies.relation_allowlist)
        plan = self.plans["commerce.join_007"]
        query = self.compiler.compile(self.compiler.bind(plan, self.seller), self.seller)
        statement = parse(query.executable_sql, read="postgres")[0]
        observed = {
            f"{table.db}.{table.name}" if table.db else table.name
            for table in statement.find_all(exp.Table)
        }

        self.assertTrue(observed)
        self.assertTrue(observed.issubset(allowed))
        for relation in observed:
            schema, table = relation.split(".", 1)
            self.assertRegex(
                query.executable_sql,
                rf'"{re.escape(schema)}"\."{re.escape(table)}"',
            )

    def test_compile_rebinds_and_rejects_every_caller_mutated_bound_surface(self) -> None:
        plan = self.plans["commerce.metric_002"]
        bound = self.compiler.bind(plan, self.seller)
        variants = (
            bound.model_copy(update={"logical_plan_hash": "0" * 64}),
            bound.model_copy(
                update={
                    "physical_relations": (
                        "public.olist_order_items_dataset",
                        "public.evil",
                    )
                }
            ),
            bound.model_copy(
                update={
                    "entities": (
                        bound.entities[0].model_copy(
                            update={"physical_relation": "public.evil"}
                        ),
                        *bound.entities[1:],
                    )
                }
            ),
            bound.model_copy(
                update={
                    "selected_columns": (
                        bound.selected_columns[0].model_copy(
                            update={"physical_column": "evil_column"}
                        ),
                        *bound.selected_columns[1:],
                    )
                }
            ),
            bound.model_copy(
                update={
                    "joins": (
                        bound.joins[0].model_copy(update={"from_columns": ("evil",)}),
                        *bound.joins[1:],
                    )
                }
            ),
            bound.model_copy(
                update={
                    "predicates": (
                        *bound.predicates[:-1],
                        bound.predicates[-1].model_copy(update={"value": "other-seller"}),
                    )
                }
            ),
            bound.model_copy(
                update={
                    "lineage": bound.lineage.model_copy(
                        update={"logical_refs": (*bound.lineage.logical_refs, "commerce.evil")}
                    )
                }
            ),
            bound.model_copy(
                update={
                    "required_access": bound.required_access.model_copy(
                        update={"allowed_relations": ("public.evil",)}
                    )
                }
            ),
        )

        for candidate in variants:
            with self.subTest(candidate=candidate), self.assertRaises(BindingError) as raised:
                self.compiler.compile(candidate, self.seller)
            self.assertEqual(
                raised.exception.code,
                BindingErrorCode.BOUND_PLAN_MISMATCH,
            )

        with self.assertRaises(BindingError) as wrong_principal:
            self.compiler.compile(bound, self.admin)
        self.assertEqual(
            wrong_principal.exception.code,
            BindingErrorCode.BOUND_PLAN_MISMATCH,
        )

    def test_compiler_parameterizes_every_structural_literal(self) -> None:
        cases = {
            "commerce.metric_001": {"month"},
            "commerce.logistics_001": {"epoch", 86400},
            "commerce.metric_010": {"month", 0},
        }
        for case_id, constants in cases.items():
            with self.subTest(case=case_id):
                bound = self.compiler.bind(self.plans[case_id], self.admin)
                prepared = self.compiler.compile(bound, self.admin)
                statement = parse(prepared.executable_sql, read="postgres")[0]
                unbound_literals = [
                    item
                    for item in statement.find_all(exp.Literal)
                    if not isinstance(item.parent, exp.Parameter)
                ]
                self.assertEqual(unbound_literals, [])
                values = {item.value for item in prepared.parameters}
                self.assertTrue(constants.issubset(values))
                self.assertIn(
                    "algorithm_constant",
                    {item.purpose for item in prepared.parameters},
                )


if __name__ == "__main__":
    unittest.main()
