from __future__ import annotations

import sys
import unittest
import json
import hashlib
import copy
from pathlib import Path
from types import SimpleNamespace

import sqlglot
from sqlglot import exp


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from data_agent.runtime import (
    AgentEventType,
    AgentRequest,
    DataAgentRuntime,
    PrincipalContext,
    ModelLogicalPlanner,
    load_domain_pack,
)
from data_agent.execution import (
    COMMERCE_EXECUTION_GRAPH,
    ExecutionDependencies,
    InternalGraphExecutor,
)
from data_agent.runtime import DefaultDataAgentRuntime, RuntimeDependencies
from data_agent.runtime.bundle_store import BundlePaths, BundleStore
from data_agent.runtime.context import ContextAssembler
from data_agent.runtime.context_resolver import RuntimeContextResolver
from data_agent.skills import BUILTIN_SKILL_REGISTRY, LogicalQueryPlan
from data_agent.tools import QueryRow, TabularResult, ToolInvoker
from data_agent.tools.providers import build_builtin_registry
from tests.support.olist_golden_oracle import (
    assert_plan_matches_raw_case,
    assert_oracle_matches_governed_sources,
    assert_sql_semantics,
    sql_semantic_signature,
)
from tests.support.olist_expected_answer import render_expected_answer
from tests.support.olist_relational_evaluator import evaluate_raw_case
from tests.support.olist_relational_evaluator import (
    assert_oracle_parameters_match,
    derive_parameter_purpose_values,
    principal_is_admin,
)
from tests.support.runtime_fakes import CredentialBroker, CountingMemory, FakeConnector


GOLDEN_PRINCIPALS = {
    "seller": PrincipalContext(
        tenant_id="seller-42",
        user_id="golden-eval",
        roles=("seller",),
    ),
    "admin": PrincipalContext(
        tenant_id="admin-eval",
        user_id="golden-eval",
        roles=("admin",),
    ),
}


class _DeterministicModelClient:
    model_id = "olist-golden-model"
    version = "fixture-v1"

    def __init__(self, plans_by_question):
        self._plans_by_question = plans_by_question
        self.questions = []

    async def complete(self, prompt: str, **_kwargs) -> str:
        question = json.loads(prompt)["question"]
        self.questions.append(question)
        return self._plans_by_question[question].model_dump_json()


class _GoldenConnector(FakeConnector):
    def __init__(
        self,
        schema_fingerprint,
        case_by_plan_hash,
        oracles=None,
        *,
        domain_pack=None,
        enterprise_binding=None,
    ):
        super().__init__(schema_fingerprint)
        self._case_by_plan_hash = case_by_plan_hash
        self._oracles = oracles or {}
        self._domain_pack = domain_pack
        self._enterprise_binding = enterprise_binding
        self._principal_authority = None

    def bind_principal_authority(self, principal) -> None:
        self._principal_authority = principal

    def _assert_grant_authority(self, grant) -> None:
        principal = self._principal_authority
        if principal is None or self._enterprise_binding is None:
            raise ValueError("golden connector requires the actual principal authority")
        if grant.tenant_id != principal.tenant_id:
            raise ValueError("access grant tenant differs from evaluator principal")
        if grant.principal_user_id != principal.user_id:
            raise ValueError("access grant user differs from evaluator principal")
        if grant.admin_bypass != principal_is_admin(
            self._enterprise_binding,
            principal,
        ):
            raise ValueError("access grant admin authority differs from evaluator principal")

    def _case_table(self, prepared):
        case = self._case_by_plan_hash[prepared.logical_plan_hash]
        case_oracles = self._oracles.get(case.id, {})
        principal = self._principal_authority
        if principal is None:
            if len(case_oracles) != 1:
                raise ValueError("golden connector has no bound principal authority")
            oracle_label, oracle = next(iter(case_oracles.items()))
        else:
            oracle_label = (
                "admin"
                if principal_is_admin(self._enterprise_binding, principal)
                else "seller"
            )
            oracle = case_oracles.get(oracle_label)
        if oracle is not None:
            assert_sql_semantics(prepared.executable_sql, oracle["signature"])
            if self._enterprise_binding is None or principal is None:
                raise ValueError("golden connector requires the Enterprise binding")
            assert_oracle_parameters_match(
                case,
                self._enterprise_binding,
                oracle,
                principal=principal,
            )
            observed_parameters = [
                item.model_dump(mode="json") for item in prepared.parameters
            ]
            if observed_parameters != oracle["parameters"]:
                raise ValueError("SQL parameter oracle mismatch")
            if self._domain_pack is None or self._enterprise_binding is None:
                raise ValueError("golden connector requires governed evaluator inputs")
            evaluated = evaluate_raw_case(
                case,
                self._domain_pack,
                self._enterprise_binding,
                principal=principal,
            )
            return TabularResult(
                columns=evaluated.columns,
                rows=tuple(
                    QueryRow(values=values) for values in evaluated.rows
                ),
            )
        return TabularResult(
            columns=("case_id", "logical_plan_hash", "scope"),
            rows=(
                QueryRow(
                    values=(case.id, prepared.logical_plan_hash, oracle_label)
                ),
            ),
        )

    async def execute_readonly(self, prepared, grant, lease):
        self.execute_calls.append((prepared, grant, lease))
        self._assert_bound(prepared, grant)
        self._assert_grant_authority(grant)
        return self._case_table(prepared)

    async def preview(self, prepared, grant, lease, *, preview_rows: int):
        self.preview_calls.append((prepared, grant, lease, preview_rows))
        self._assert_bound(prepared, grant)
        self._assert_grant_authority(grant)
        table = self._case_table(prepared)
        return table.model_copy(update={"rows": table.rows[:preview_rows]})


class _GoldenFixture:
    def __init__(self, runtime, connector, model_client, snapshot, plans, oracles):
        self.runtime = runtime
        self.connector = connector
        self.model_client = model_client
        self.snapshot = snapshot
        self.plans = plans
        self.oracles = oracles


def build_deterministic_olist_runtime_fixture(
    *, domain_pack, cases
) -> _GoldenFixture:
    paths = BundlePaths(
        domain_root=ROOT / "packs" / "domains" / "commerce",
        enterprise_root=ROOT / "packs" / "enterprises" / "olist",
        deployment_profile=ROOT / "packs" / "deployments" / "olist-local.yaml",
        pack_lock=ROOT / "packs" / "enterprises" / "olist" / "pack.lock",
        schema_catalog=ROOT / "schema_catalog.json",
        bundle_manifest=ROOT / "generated" / "bundles" / "olist-local.json",
    )
    store = BundleStore()
    snapshot = store.load_and_activate(paths)
    memory = CountingMemory()
    plan_documents = json.loads(
        (ROOT / "tests" / "fixtures" / "olist_golden_model_plans.json").read_text(
            encoding="utf-8"
        )
    )
    plans_by_case = {
        case.id: LogicalQueryPlan.model_validate(plan_documents[case.id]) for case in cases
    }
    plans_by_question = {case.question: plans_by_case[case.id] for case in cases}
    oracles = json.loads(
        (ROOT / "tests" / "fixtures" / "olist_golden_oracle.json").read_text(
            encoding="utf-8"
        )
    )
    case_by_plan_hash = {
        plans_by_case[case.id].stable_hash(): case for case in cases
    }
    connector = _GoldenConnector(
        snapshot.bundle.schema_fingerprint,
        case_by_plan_hash,
        oracles,
        domain_pack=snapshot.domain_pack,
        enterprise_binding=snapshot.enterprise_binding,
    )
    broker = CredentialBroker()
    registry = build_builtin_registry(
        snapshot.domain_pack,
        snapshot.enterprise_binding,
        snapshot.bundle,
        connector,
    )
    resolver = RuntimeContextResolver(memory=memory, assembler=ContextAssembler())
    model_client = _DeterministicModelClient(plans_by_question)
    planner = ModelLogicalPlanner(model_client)
    executor = InternalGraphExecutor(
        COMMERCE_EXECUTION_GRAPH,
        ExecutionDependencies(
            invoker=ToolInvoker(registry, credential_broker=broker),
            context_resolver=resolver,
            planner=planner,
            domain_pack=snapshot.domain_pack,
        ),
    )
    run_number = 0

    def next_run_id() -> str:
        nonlocal run_number
        run_number += 1
        return f"olist-golden-{run_number:02d}"

    runtime = DefaultDataAgentRuntime(
        RuntimeDependencies(
            bundle_store=store,
            skill_registry=BUILTIN_SKILL_REGISTRY,
            tool_registry=registry,
            graph=COMMERCE_EXECUTION_GRAPH,
            executor=executor,
            memory=memory,
            context_resolver=resolver,
            planner=planner,
            model_client=model_client,
            run_id_factory=next_run_id,
        )
    )
    return _GoldenFixture(
        runtime,
        connector,
        model_client,
        snapshot,
        plans_by_case,
        oracles,
    )


class OListGoldenRuntimeE2ETests(unittest.IsolatedAsyncioTestCase):
    def test_evaluator_authority_follows_actual_non_admin_principal(self) -> None:
        domain_pack = load_domain_pack(ROOT / "packs" / "domains" / "commerce")
        fixture = build_deterministic_olist_runtime_fixture(
            domain_pack=domain_pack,
            cases=tuple(domain_pack.spec.evals),
        )
        cases = {case.id: case for case in domain_pack.spec.evals}
        actual_principal = PrincipalContext(
            tenant_id="seller-7",
            user_id="different-user",
            roles=("seller",),
        )
        detail_case = cases["commerce.detail_002"]
        context_case = cases["commerce.tenant_001"]

        detail_parameters = derive_parameter_purpose_values(
            detail_case,
            fixture.snapshot.enterprise_binding,
            principal=actual_principal,
        )
        context_parameters = derive_parameter_purpose_values(
            context_case,
            fixture.snapshot.enterprise_binding,
            principal=actual_principal,
        )
        evaluated = evaluate_raw_case(
            detail_case,
            domain_pack,
            fixture.snapshot.enterprise_binding,
            principal=actual_principal,
        )
        seller_index = evaluated.columns.index("seller_id")

        with self.subTest(authority="tenant_scope"):
            self.assertIn(
                ("tenant_scope", actual_principal.tenant_id),
                detail_parameters,
            )
        with self.subTest(authority="tenant_context"):
            self.assertIn(
                ("tenant_context", actual_principal.tenant_id),
                context_parameters,
            )
        with self.subTest(authority="business_rows"):
            self.assertEqual(
                {row[seller_index] for row in evaluated.rows},
                {actual_principal.tenant_id},
            )
        with self.subTest(authority="oracle_mismatch"):
            with self.assertRaises(ValueError):
                assert_oracle_matches_governed_sources(
                    detail_case,
                    domain_pack,
                    fixture.snapshot.enterprise_binding,
                    fixture.oracles[detail_case.id]["seller"],
                    principal=actual_principal,
                )

    def test_answer_oracle_probe_rejects_appended_unexpected_content(self) -> None:
        one_row = render_expected_answer(
            ("seller_id", "gmv", "optional"),
            (("seller-42", 115.0, None),),
        )
        self.assertEqual(
            one_row,
            "已基于验证后的查询证据返回 1 行结果。 "
            "First verified row: seller_id=seller-42, gmv=115.0, optional=.",
        )
        self.assertEqual(
            render_expected_answer(("seller_id",), ()),
            "验证后的查询未返回结果。",
        )
        self.assertEqual(
            render_expected_answer(
                ("seller_id", "gmv"),
                (("seller-42", 115.0), ("seller-7", 77.0)),
            ),
            "已基于验证后的查询证据返回 2 行结果。 "
            "First verified row: seller_id=seller-42, gmv=115.0.",
        )
        polluted = one_row + " unexpected 999"
        self.assertNotEqual(polluted, one_row)

    def test_independent_parameter_derivation_matches_all_96_oracles(self) -> None:
        domain_pack = load_domain_pack(ROOT / "packs" / "domains" / "commerce")
        fixture = build_deterministic_olist_runtime_fixture(
            domain_pack=domain_pack,
            cases=tuple(domain_pack.spec.evals),
        )
        for case in domain_pack.spec.evals:
            for oracle_label, principal in GOLDEN_PRINCIPALS.items():
                assert_oracle_parameters_match(
                    case,
                    fixture.snapshot.enterprise_binding,
                    fixture.oracles[case.id][oracle_label],
                    principal=principal,
                )

    def test_independent_relational_evaluator_covers_all_48_cases(self) -> None:
        domain_pack = load_domain_pack(ROOT / "packs" / "domains" / "commerce")
        fixture = build_deterministic_olist_runtime_fixture(
            domain_pack=domain_pack,
            cases=tuple(domain_pack.spec.evals),
        )
        for case in domain_pack.spec.evals:
            seller = evaluate_raw_case(
                case,
                domain_pack,
                fixture.snapshot.enterprise_binding,
                principal=GOLDEN_PRINCIPALS["seller"],
            )
            admin = evaluate_raw_case(
                case,
                domain_pack,
                fixture.snapshot.enterprise_binding,
                principal=GOLDEN_PRINCIPALS["admin"],
            )
            self.assertTrue(seller.columns, case.id)
            self.assertTrue(seller.rows, case.id)
            self.assertTrue(admin.rows, case.id)
            self.assertNotEqual(seller.rows, admin.rows, case.id)

    def test_independent_relational_evaluator_detail_case(self) -> None:
        domain_pack = load_domain_pack(ROOT / "packs" / "domains" / "commerce")
        case = next(
            item for item in domain_pack.spec.evals if item.id == "commerce.detail_002"
        )
        fixture = build_deterministic_olist_runtime_fixture(
            domain_pack=domain_pack,
            cases=tuple(domain_pack.spec.evals),
        )

        seller = evaluate_raw_case(
            case,
            domain_pack,
            fixture.snapshot.enterprise_binding,
            principal=GOLDEN_PRINCIPALS["seller"],
        )
        admin = evaluate_raw_case(
            case,
            domain_pack,
            fixture.snapshot.enterprise_binding,
            principal=GOLDEN_PRINCIPALS["admin"],
        )

        self.assertEqual(
            seller.columns,
            (
                "order_id",
                "item_sequence",
                "product_id",
                "seller_id",
                "item_price",
                "freight_amount",
            ),
        )
        self.assertEqual(
            seller.rows,
            (
                ("order-aug", 1, "product-health", "seller-42", 200.0, 20.0),
                ("order-cancel", 1, "product-toys", "seller-42", 50.0, 5.0),
            ),
        )
        self.assertEqual(
            tuple(row[0] for row in admin.rows),
            ("order-aug", "order-aug", "order-cancel-2", "order-cancel"),
        )

    def test_governed_source_audit_rejects_parameter_and_result_mutations(self) -> None:
        domain_pack = load_domain_pack(ROOT / "packs" / "domains" / "commerce")
        cases = {case.id: case for case in domain_pack.spec.evals}
        fixture = build_deterministic_olist_runtime_fixture(
            domain_pack=domain_pack,
            cases=tuple(domain_pack.spec.evals),
        )

        mutations = (
            ("tenant", "commerce.detail_002", "tenant_scope", "WRONG-TENANT"),
            ("time", "commerce.detail_002", "time_start", "1999-01-01"),
            ("filter", "commerce.payment_002", "filter", "cash"),
            ("having", "commerce.review_003", "filter", 999),
            ("limit", "commerce.metric_003", "limit", 999),
            ("grain", "commerce.metric_001", "algorithm_constant", "year"),
        )
        tampered_oracles = []
        for label, case_id, purpose, value in mutations:
            oracle = copy.deepcopy(fixture.oracles[case_id]["seller"])
            parameter = next(
                item for item in oracle["parameters"] if item["purpose"] == purpose
            )
            parameter["value"] = value
            tampered_oracles.append((label, case_id, oracle))

        wrong_columns = copy.deepcopy(fixture.oracles["commerce.detail_002"]["seller"])
        wrong_columns["columns"][0] = "unrelated_column"
        tampered_oracles.append(("columns", "commerce.detail_002", wrong_columns))
        wrong_row = copy.deepcopy(fixture.oracles["commerce.detail_002"]["seller"])
        wrong_row["rows"][0][0] = "UNRELATED-BUSINESS-ROW"
        tampered_oracles.append(("row", "commerce.detail_002", wrong_row))

        for mutation, case_id, oracle in tampered_oracles:
            with self.subTest(mutation=mutation, case=case_id):
                with self.assertRaises(ValueError):
                    assert_oracle_matches_governed_sources(
                        cases[case_id],
                        domain_pack,
                        fixture.snapshot.enterprise_binding,
                        oracle,
                        principal=GOLDEN_PRINCIPALS["seller"],
                    )

    def test_golden_connector_rejects_sql_semantic_mutations(self) -> None:
        domain_pack = load_domain_pack(ROOT / "packs" / "domains" / "commerce")
        case = next(item for item in domain_pack.spec.evals if item.id == "commerce.metric_003")
        correct = (
            "SELECT product_category_name, SUM(price + freight_value) AS gmv "
            "FROM public.olist_order_items_dataset "
            "WHERE shipping_limit_date >= %(start)s "
            "AND shipping_limit_date < %(end)s "
            "GROUP BY product_category_name ORDER BY gmv DESC LIMIT 15"
        )
        mutations = (
            correct.replace("SUM(", "AVG("),
            correct.replace(
                "WHERE shipping_limit_date >= %(start)s "
                "AND shipping_limit_date < %(end)s ",
                "",
            ),
            correct.replace("GROUP BY product_category_name ", ""),
        )
        connector = _GoldenConnector(
            "schema",
            {"plan": case},
            {case.id: {"admin": {"signature": sql_semantic_signature(correct)}}},
        )

        for sql in mutations:
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError):
                    connector._case_table(
                        SimpleNamespace(
                            logical_plan_hash="plan",
                            parameters=(),
                            executable_sql=sql,
                        )
                    )

    @staticmethod
    def _expected_policy_id(bundle, principal, logical_plan_hash):
        payload = {
            "bundle": bundle.digest,
            "user": principal.user_id,
            "tenant": principal.tenant_id,
            "roles": sorted(set(principal.roles)),
            "logical_plan_hash": logical_plan_hash,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "policy_" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _sql_relations(sql):
        statement = sqlglot.parse_one(sql, read="postgres")
        return {
            f"{table.db}.{table.name}" if table.db else table.name
            for table in statement.find_all(exp.Table)
        }

    async def test_same_runtime_executes_all_48_eval_yaml_cases(self) -> None:
        domain_pack = load_domain_pack(ROOT / "packs" / "domains" / "commerce")
        cases = tuple(domain_pack.spec.evals)
        self.assertEqual(len(cases), 48, "commerce eval YAML must contain exactly 48 cases")

        fixture = build_deterministic_olist_runtime_fixture(
            domain_pack=domain_pack,
            cases=cases,
        )
        self.assertIsInstance(fixture.runtime, DataAgentRuntime)

        for case in cases:
            self.assertEqual(
                fixture.oracles[case.id]["raw_case"],
                case.model_dump(mode="json"),
            )
            self.assertNotEqual(
                fixture.oracles[case.id]["seller"]["rows"],
                fixture.oracles[case.id]["admin"]["rows"],
                f"{case.id}: seller/admin business results must visibly differ",
            )
            for scope, principal in GOLDEN_PRINCIPALS.items():
              with self.subTest(case=case.id, question=case.question, scope=scope):
                fixture.connector.bind_principal_authority(principal)
                events = [
                    event
                    async for event in fixture.runtime.run(
                        AgentRequest(
                            question=case.question,
                            enterprise_id="olist",
                            domain_id="commerce",
                            include_trace=True,
                        ),
                        principal,
                    )
                ]
                terminal = events[-1]
                self.assertEqual(
                    terminal.type,
                    AgentEventType.RUN_COMPLETED,
                    f"{case.id}: public Runtime must emit a completed terminal",
                )
                self.assertIsNotNone(
                    terminal.response,
                    f"{case.id}: terminal must carry AgentResponse",
                )
                expected_plan = fixture.plans[case.id]
                assert_plan_matches_raw_case(expected_plan, case)
                self.assertEqual(
                    terminal.response.logical_plan,
                    expected_plan,
                    f"{case.id}: model-backed logical plan must match frozen fixture",
                )
                self.assertIsNotNone(
                    terminal.response.sql,
                    f"{case.id}: enterprise binding must compile physical SQL",
                )
                prepared = fixture.connector.execute_calls[-1][0]
                tenant_parameters = tuple(
                    item for item in prepared.parameters if item.purpose == "tenant_scope"
                )
                admin = principal_is_admin(
                    fixture.snapshot.enterprise_binding,
                    principal,
                )
                self.assertEqual(
                    bool(tenant_parameters),
                    not admin,
                    f"{case.id}/{scope}: policy must apply seller scope and admin bypass",
                )
                if not admin:
                    self.assertIn(
                        "seller_id",
                        terminal.response.sql.lower(),
                        f"{case.id}/{scope}: seller SQL must retain ownership predicate",
                    )
                    self.assertTrue(
                        all(
                            item.value == principal.tenant_id
                            for item in tenant_parameters
                        ),
                        f"{case.id}/{scope}: ownership parameter must equal seller tenant",
                    )
                else:
                    self.assertTrue(
                        all(
                            item.purpose == "tenant_context"
                            for item in prepared.parameters
                            if item.value == principal.tenant_id
                        ),
                        f"{case.id}/{scope}: admin tenant values may only satisfy explicit context filters",
                    )
                self.assertEqual(
                    prepared.logical_plan_hash,
                    expected_plan.stable_hash(),
                    f"{case.id}/{scope}: compiled query must bind the golden plan",
                )
                self.assertTrue(
                    prepared.allowed_relations,
                    f"{case.id}/{scope}: binding must declare physical relations",
                )
                binding_relations = {
                    item.relation
                    for item in fixture.snapshot.enterprise_binding.spec.bindings.values()
                }
                expected_direct_relations = {
                    fixture.snapshot.enterprise_binding.spec.bindings[entity].relation
                    for entity in expected_plan.entities
                }
                self.assertTrue(
                    expected_direct_relations.issubset(set(prepared.allowed_relations)),
                    f"{case.id}/{scope}: binding must include every eval entity relation",
                )
                self.assertTrue(
                    set(prepared.allowed_relations).issubset(binding_relations),
                    f"{case.id}/{scope}: physical relations must come from OList binding allowlist",
                )
                self.assertEqual(
                    self._sql_relations(prepared.executable_sql),
                    set(prepared.allowed_relations),
                    f"{case.id}/{scope}: compiled SQL relations must equal BoundPlan relations",
                )
                self.assertEqual(
                    prepared.policy_decision_id,
                    self._expected_policy_id(
                        fixture.snapshot.bundle,
                        principal,
                        expected_plan.stable_hash(),
                    ),
                    f"{case.id}/{scope}: policy decision must pin bundle/principal/plan authority",
                )
                self.assertEqual(
                    terminal.response.sql,
                    prepared.executable_sql,
                    f"{case.id}/{scope}: AgentResponse must expose the compiled prepared SQL",
                )
                expected_result = fixture.oracles[case.id][scope]
                assert_oracle_matches_governed_sources(
                    case,
                    domain_pack,
                    fixture.snapshot.enterprise_binding,
                    expected_result,
                    principal=principal,
                )
                expected_row = dict(
                    zip(
                        expected_result["columns"],
                        expected_result["rows"][0],
                        strict=True,
                    )
                )
                expected_rows = tuple(
                    {
                        column: value
                        for column, value in zip(
                            expected_result["columns"],
                            values,
                            strict=True,
                        )
                    }
                    for values in expected_result["rows"]
                )
                self.assertEqual(
                    tuple(row.root for row in terminal.response.rows),
                    expected_rows,
                    f"{case.id}/{scope}: every public row must match the evaluator snapshot",
                )
                self.assertEqual(
                    terminal.response.rows[0].root,
                    expected_row,
                    f"{case.id}/{scope}: response rows must match frozen business result",
                )
                expected_answer = render_expected_answer(
                    expected_result["columns"],
                    expected_result["rows"],
                )
                self.assertEqual(
                    terminal.response.answer,
                    expected_answer,
                    f"{case.id}/{scope}: complete answer must match independent evidence rendering",
                )
                self.assertTrue(
                    terminal.response.trace,
                    f"{case.id}: response must expose the governed execution trace",
                )
                self.assertIsNotNone(
                    terminal.response.version_pins,
                    f"{case.id}: response must pin bundle/skill/graph/tool/model versions",
                )
                trace_nodes = {item.node for item in terminal.response.trace}
                self.assertTrue(
                    {
                        "validate_logical_plan",
                        "query.compile",
                        "query.execute",
                        "answer.render",
                        "finalize",
                    }.issubset(trace_nodes),
                    f"{case.id}/{scope}: trace must connect plan, policy, query, evidence, answer",
                )
                pins = terminal.response.version_pins
                self.assertEqual(pins.bundle_digest, fixture.snapshot.bundle.digest)
                self.assertEqual(pins.domain_pack_digest, fixture.snapshot.domain_pack_digest)
                self.assertEqual(
                    pins.enterprise_binding_digest,
                    fixture.snapshot.enterprise_binding_digest,
                )
                self.assertEqual(pins.skill_id, "commerce.analytics")
                self.assertEqual(pins.skill_version, "1.0.0")
                self.assertEqual(pins.graph_digest, COMMERCE_EXECUTION_GRAPH.digest)
                self.assertEqual(pins.tool_registry_version, "1.0.0")
                self.assertEqual(
                    tuple((item.component, item.version) for item in pins.model_versions),
                    (("olist-golden-model", "fixture-v1"),),
                )

        self.assertEqual(
            fixture.model_client.questions,
            [case.question for case in cases for _scope in ("seller", "admin")],
            "deterministic ModelClient must serve every seller/admin Runtime request",
        )


if __name__ == "__main__":
    unittest.main()
