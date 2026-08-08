from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from data_agent.relationships.models import (
    RelationshipCondition,
    RelationshipComponent,
    RelationshipEdge,
    RelationshipEdgeQuality,
    RelationshipGraphDraft,
    RelationshipGraphNode,
    RelationshipProvenance,
    RelationshipRouteRule,
)
from data_agent.relationships.router import GraphRouteError, GraphRouteRequest, GraphRouteResolver
from data_agent.relationships.recommender import RelationshipRecommender
from data_agent.relationships.grain import FanoutGuard
from data_agent.relationships.validator import validate_graph
from data_agent.relationships.profiler import PairProfile, ProfileBudget, profile_pair, profile_values
from data_agent.relationships.candidates import prefilter_candidates
from data_agent.relationships.models import ActivatedRelationshipGraph
from data_agent.datasources import SemanticGraphBindingRecord, SemanticGraphFieldMapping, SemanticBindingStatus
from data_agent.tools.schemas import CatalogColumn, CatalogKey, CatalogRelation, CatalogSnapshot, catalog_schema_fingerprint
from data_agent.dataset_query import DatasetQueryCompiler, DatasetQueryPlan


def _graph(edges: tuple[RelationshipEdge, ...]) -> RelationshipGraphDraft:
    nodes = tuple(
        RelationshipGraphNode(node_id=node, relation_id=f"relation:{node}", role_name=node, logical_entity=f"Entity{node}")
        for node in ("a", "b", "c", "d")
    )
    return RelationshipGraphDraft(graph_id="graph", tenant_id="tenant", source_id="source", source_snapshot_version=1, schema_fingerprint="sha256:test", revision=1, status="draft", nodes=nodes, edges=edges, components=())


def _edge(edge_id: str, left: str, right: str) -> RelationshipEdge:
    return RelationshipEdge(edge_id=edge_id, from_node_id=left, to_node_id=right, conditions=(RelationshipCondition(from_column_id=f"column:{left}", to_column_id=f"column:{right}"),), cardinality="many_to_one", provenance=RelationshipProvenance(source="user"))


class RelationshipGraphTests(unittest.IsolatedAsyncioTestCase):
    def test_cycle_parallel_and_self_role_edges_are_valid(self) -> None:
        graph = _graph((_edge("ab", "a", "b"), _edge("ab_alt", "a", "b"), _edge("bc", "b", "c"), _edge("ca", "c", "a")))
        self.assertEqual(len(graph.edges), 4)

    def test_left_requires_preserve_endpoint(self) -> None:
        with self.assertRaises(ValidationError):
            _edge("ab", "a", "b").model_copy(update={"join_semantics": "left"})

    def test_diamond_returns_typed_ambiguity(self) -> None:
        graph = _graph((_edge("ab", "a", "b"), _edge("bd", "b", "d"), _edge("ac", "a", "c"), _edge("cd", "c", "d")))
        with self.assertRaises(GraphRouteError) as captured:
            GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "d")))
        self.assertEqual(captured.exception.code, "GRAPH_AMBIGUOUS_PATH")

    def test_simple_chain_is_deterministic(self) -> None:
        graph = _graph((_edge("ab", "a", "b"), _edge("bc", "b", "c")))
        route = GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "c")))
        self.assertEqual([step.edge_id for step in route.steps], ["ab", "bc"])

    def test_explicit_route_rule_resolves_a_diamond(self) -> None:
        graph = _graph((_edge("ab", "a", "b"), _edge("bd", "b", "d"), _edge("ac", "a", "c"), _edge("cd", "c", "d"))).model_copy(
            update={"route_rules": (RelationshipRouteRule(rule_id="via-b", terminal_node_ids=("a", "d"), ordered_edge_ids=("ab", "bd")),)}
        )
        route = GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "d")))
        self.assertEqual(route.route_rule_id, "via-b")
        self.assertEqual([step.edge_id for step in route.steps], ["ab", "bd"])

    def test_parallel_edges_remain_ambiguous_without_a_rule(self) -> None:
        graph = _graph((_edge("ab", "a", "b"), _edge("ab_alt", "a", "b")))
        with self.assertRaises(GraphRouteError) as captured:
            GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "b")))
        self.assertEqual(captured.exception.code, "GRAPH_AMBIGUOUS_PATH")

    def test_customer_and_account_parallel_edges_stay_separate(self) -> None:
        graph = _graph((
            RelationshipEdge(edge_id="customer", from_node_id="a", to_node_id="b", conditions=(RelationshipCondition(from_column_id="column:customer_id", to_column_id="column:customer_id_2"),), cardinality="many_to_one", provenance=RelationshipProvenance(source="user")),
            RelationshipEdge(edge_id="account", from_node_id="a", to_node_id="b", conditions=(RelationshipCondition(from_column_id="column:account_id", to_column_id="column:account_id_2"),), cardinality="many_to_one", provenance=RelationshipProvenance(source="user")),
        ))
        self.assertEqual({edge.edge_id for edge in graph.edges}, {"customer", "account"})
        with self.assertRaises(GraphRouteError) as captured:
            GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "b")))
        self.assertEqual(captured.exception.code, "GRAPH_AMBIGUOUS_PATH")

    def test_left_edge_can_only_expand_from_preserved_role(self) -> None:
        edge = _edge("ab", "a", "b").model_copy(update={"join_semantics": "left", "preserve_node_id": "a"})
        graph = _graph((edge,))
        self.assertEqual(
            [step.edge_id for step in GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "b"))).steps],
            ["ab"],
        )
        with self.assertRaises(GraphRouteError) as captured:
            GraphRouteResolver().resolve(graph, GraphRouteRequest(("b", "a")))
        self.assertEqual(captured.exception.code, "GRAPH_NO_PATH")

    def test_cycle_and_disconnected_components_are_handled_safely(self) -> None:
        graph = _graph((_edge("ab", "a", "b"), _edge("bc", "b", "c"), _edge("ca", "c", "a")))
        route = GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "c")))
        self.assertEqual(len(route.steps), 1)
        with self.assertRaises(GraphRouteError) as captured:
            GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "d")))
        self.assertEqual(captured.exception.code, "GRAPH_NO_PATH")

    def test_star_route_uses_only_required_branches(self) -> None:
        graph = _graph((_edge("ab", "a", "b"), _edge("ac", "a", "c"), _edge("ad", "a", "d")))
        route = GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "b", "c")))
        self.assertEqual([step.edge_id for step in route.steps], ["ab", "ac"])
        self.assertNotIn("d", route.included_node_ids)

    def test_disabled_or_blocked_edges_cannot_form_a_route(self) -> None:
        disabled = _edge("ab", "a", "b").model_copy(update={"enabled": False})
        with self.assertRaises(GraphRouteError) as captured:
            GraphRouteResolver().resolve(_graph((disabled,)), GraphRouteRequest(("a", "b")))
        self.assertEqual(captured.exception.code, "GRAPH_NO_PATH")
        blocked = _edge("ab", "a", "b").model_copy(
            update={"quality": {"evidence_level": "blocked"}}
        )
        with self.assertRaises(GraphRouteError) as captured:
            GraphRouteResolver().resolve(
                _graph((blocked,)),
                GraphRouteRequest(("a", "b")),
                findings=validate_graph(_graph((blocked,))).findings,
            )
        self.assertEqual(captured.exception.code, "GRAPH_NO_PATH")

    async def test_recommender_discards_hallucinated_ids(self) -> None:
        class FakeModel:
            model_id = "fake"
            version = "1"
            def __init__(self) -> None:
                self.prompt = ""
            async def complete(self, *_args, **_kwargs):
                self.prompt = str(_args[0])
                return '{"recommendations":[{"from_relation_id":"bad","from_column_id":"bad","to_relation_id":"bad","to_column_id":"bad","confidence":1,"explanation":"bad"}]}'
        catalog = CatalogSnapshot(schema_fingerprint="sha256:test", relations=(
            CatalogRelation(relation="main.orders", columns=(CatalogColumn(name="customer_id", data_type="INTEGER", nullable=False),)),
            CatalogRelation(relation="main.customers", columns=(CatalogColumn(name="customer_id", data_type="INTEGER", nullable=False),)),
        ))
        model = FakeModel()
        self.assertEqual(await RelationshipRecommender().recommend(catalog=catalog, model_client=model), ())
        self.assertNotIn('"values":', model.prompt)
        self.assertNotIn("credentials", model.prompt)
        self.assertNotIn("password", model.prompt)

    async def test_recommender_repairs_once_and_caches_by_snapshot(self) -> None:
        class FakeModel:
            model_id = "fake"
            version = "1"
            def __init__(self) -> None:
                self.calls = 0
            async def complete(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return "not json"
                return json.dumps({"recommendations": [{"from_relation_id": "relation:a", "from_column_id": "column:a", "to_relation_id": "relation:b", "to_column_id": "column:b", "cardinality_hint": "many_to_one", "confidence": .9, "explanation": "compatible IDs"}]})
        catalog = CatalogSnapshot(schema_fingerprint="sha256:repair", relations=(
            CatalogRelation(relation_id="relation:a", relation="main.a", columns=(CatalogColumn(column_id="column:a", name="customer_id", data_type="INTEGER", nullable=False),)),
            CatalogRelation(relation_id="relation:b", relation="main.b", columns=(CatalogColumn(column_id="column:b", name="customer_id", data_type="INTEGER", nullable=False),)),
        ))
        model = FakeModel()
        recommender = RelationshipRecommender()
        result = await recommender.recommend(catalog=catalog, model_client=model)
        self.assertEqual(len(result), 1)
        self.assertEqual(model.calls, 2)
        self.assertEqual(await recommender.recommend(catalog=catalog, model_client=model), result)
        self.assertEqual(model.calls, 2)

    def test_aggregate_fails_when_measure_crosses_one_to_many(self) -> None:
        edge = _edge("ab", "a", "b").model_copy(update={"cardinality": "one_to_many"})
        graph = _graph((edge,))
        route = GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "b")))
        decision = FanoutGuard().assess(graph=graph, route=route, measure_node_ids=("a",), analysis_type="aggregate")
        self.assertFalse(decision.safe)
        self.assertTrue(decision.expands_measure)
        self.assertEqual(decision.cardinality.node_cardinality, (("a", "one"), ("b", "many")))
        self.assertTrue(decision.preaggregation_required)
        self.assertEqual(decision.preaggregation_contract.expanding_edge_ids, ("ab",))

    def test_measure_native_grain_is_declared_and_preaggregation_is_auditable(self) -> None:
        edge = _edge("ab", "a", "b").model_copy(update={"cardinality": "one_to_many"})
        graph = _graph((edge,)).model_copy(update={
            "components": (RelationshipComponent(
                component_id="a-grain", anchor_node_id="a", grain_column_ids=("column:a",)
            ),),
        })
        route = GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "b")))
        decision = FanoutGuard().assess(graph=graph, route=route, measure_node_ids=("a",), analysis_type="aggregate")

        self.assertEqual(decision.measure_native_grains[0].grain_column_ids, ("column:a",))
        self.assertEqual(decision.preaggregation_contract.aggregation_boundary_node_ids, ("a",))

    def test_aggregate_fails_closed_for_many_to_many_without_bridge_rules(self) -> None:
        edge = _edge("ab", "a", "b").model_copy(update={"cardinality": "many_to_many"})
        graph = _graph((edge,))
        route = GraphRouteResolver().resolve(graph, GraphRouteRequest(("a", "b")))
        decision = FanoutGuard().assess(graph=graph, route=route, measure_node_ids=("a",), analysis_type="aggregate")
        self.assertFalse(decision.safe)
        self.assertIn("many_to_many", decision.reason)

    def test_low_profile_overlap_blocks_an_llm_high_confidence_edge(self) -> None:
        edge = _edge("ab", "a", "b").model_copy(update={
            "provenance": RelationshipProvenance(source="llm", run_id="run-1"),
            "quality": RelationshipEdgeQuality(evidence_level="high", match_rate=0.05),
        })
        report = validate_graph(_graph((edge,)))

        self.assertFalse(report.activation_allowed)
        self.assertEqual(report.edge_quality[0][1].evidence_level, "blocked")

    def test_profiled_different_names_can_be_allowlisted_for_llm(self) -> None:
        catalog = CatalogSnapshot(schema_fingerprint="sha256:profiles", relations=(
            CatalogRelation(relation_id="relation:orders", relation="main.orders", columns=(CatalogColumn(column_id="column:buyer", name="buyer_id", data_type="INTEGER", nullable=False),)),
            CatalogRelation(relation_id="relation:customers", relation="main.customers", columns=(CatalogColumn(column_id="column:customer", name="customer_id", data_type="INTEGER", nullable=False),)),
        ))
        profile = PairProfile(True, 0.5, 0.95, 0.05, 0.05, False, True, 1, 1, 10, 1)
        candidates = prefilter_candidates(catalog, pair_profiles={("column:buyer", "column:customer"): profile})

        self.assertEqual(candidates[0].reason_code, "PROFILE_SEMANTIC_VALUE_MATCH")

    async def test_profiled_different_names_can_be_recommended_by_llm(self) -> None:
        class FakeModel:
            model_id = "fake"
            version = "1"

            async def complete(self, *_args, **_kwargs):
                return json.dumps({"recommendations": [{
                    "from_relation_id": "relation:orders", "from_column_id": "column:buyer",
                    "to_relation_id": "relation:customers", "to_column_id": "column:customer",
                    "cardinality_hint": "many_to_one", "confidence": 0.8,
                    "explanation": "buyer and customer have compatible identifier domains",
                }]})

        catalog = CatalogSnapshot(schema_fingerprint="sha256:profiles-llm", relations=(
            CatalogRelation(relation_id="relation:orders", relation="main.orders", columns=(CatalogColumn(column_id="column:buyer", name="buyer_id", data_type="INTEGER", nullable=False),)),
            CatalogRelation(relation_id="relation:customers", relation="main.customers", columns=(CatalogColumn(column_id="column:customer", name="customer_id", data_type="INTEGER", nullable=False),)),
        ))
        profile = PairProfile(True, 0.5, 0.95, 0.05, 0.05, False, True, 1, 1, 10, 1)
        recommendations = await RelationshipRecommender().recommend(
            catalog=catalog,
            model_client=FakeModel(),
            pair_profiles={("column:buyer", "column:customer"): profile},
        )

        self.assertEqual(len(recommendations), 1)

    def test_many_to_many_bridge_route_requires_explicit_aggregate_grain(self) -> None:
        nodes = (
            RelationshipGraphNode(node_id="orders", relation_id="relation:orders", role_name="orders", logical_entity="Orders"),
            RelationshipGraphNode(node_id="order_promotions", relation_id="relation:order_promotions", role_name="order_promotions", logical_entity="OrderPromotions"),
            RelationshipGraphNode(node_id="promotions", relation_id="relation:promotions", role_name="promotions", logical_entity="Promotions"),
        )
        graph = RelationshipGraphDraft(
            graph_id="promotion-bridge", tenant_id="tenant", source_id="source", source_snapshot_version=1,
            schema_fingerprint="sha256:bridge", revision=1, status="draft", nodes=nodes,
            edges=(
                _edge("order_bridge", "orders", "order_promotions").model_copy(update={"cardinality": "one_to_many"}),
                _edge("bridge_promotion", "order_promotions", "promotions").model_copy(update={"cardinality": "many_to_one"}),
            ), components=(),
        )
        route = GraphRouteResolver().resolve(graph, GraphRouteRequest(("orders", "promotions")))
        self.assertEqual([step.edge_id for step in route.steps], ["order_bridge", "bridge_promotion"])
        decision = FanoutGuard().assess(graph=graph, route=route, measure_node_ids=("orders",), analysis_type="aggregate")
        self.assertFalse(decision.safe)
        self.assertTrue(decision.expands_measure)

    def test_bounded_profiler_returns_statistics_without_raw_values(self) -> None:
        profile = profile_values([1, 2, 2, None, 9], name="customer_id")
        pair = profile_pair([1, 2, 2, 3], [2, 2, 4], left_name="customer_id", right_name="customer_id")
        limited = profile_values(range(20), budget=ProfileBudget(max_values_per_column=3))

        self.assertEqual((profile.row_count, profile.null_count, profile.distinct_count), (5, 1, 3))
        self.assertEqual(profile.normalized_name_tokens, ("customer", "id"))
        self.assertTrue(pair.type_compatible)
        self.assertEqual(pair.estimated_joined_rows, 4)
        self.assertEqual(pair.maximum_fanout, 2)
        self.assertEqual(limited.row_count, 3)

    def test_profiler_does_not_exhaust_an_unbounded_input(self) -> None:
        def values():
            yield 1
            yield 2
            yield 3
            raise AssertionError("profile budget was ignored")

        profile = profile_values(values(), budget=ProfileBudget(max_values_per_column=3))
        self.assertEqual(profile.row_count, 3)

    def test_profiler_handles_empty_null_high_cardinality_and_sampling_failure(self) -> None:
        empty = profile_values(())
        nulls = profile_values([None, None])
        high_cardinality = profile_values(range(100), budget=ProfileBudget(max_values_per_column=5))

        def broken_values():
            raise RuntimeError("sample unavailable")
            yield 1

        failed = profile_values(broken_values())
        self.assertEqual(empty.type_family, "unknown")
        self.assertEqual((nulls.row_count, nulls.null_rate), (2, 1.0))
        self.assertEqual((high_cardinality.row_count, high_cardinality.distinct_count), (5, 5))
        self.assertEqual(failed.type_family, "unknown")

    def test_profiler_fails_closed_for_incompatible_values(self) -> None:
        pair = profile_pair([1, 2], ["1", "2"], left_name="id", right_name="id")
        self.assertFalse(pair.type_compatible)
        self.assertIsNone(pair.match_rate)

    def test_candidate_prefilter_prioritizes_key_compatible_fields(self) -> None:
        left = CatalogRelation(
            relation="main.customers",
            columns=(
                CatalogColumn(name="customer_code", data_type="TEXT", nullable=False),
                CatalogColumn(name="label", data_type="TEXT", nullable=False),
            ),
        )
        left = left.model_copy(update={"keys": (CatalogKey(kind="primary", column_ids=(left.columns[0].column_id,)),)})
        right = CatalogRelation(
            relation="main.orders",
            columns=(CatalogColumn(name="buyer_ref", data_type="TEXT", nullable=False),),
        )
        candidates = prefilter_candidates(CatalogSnapshot(schema_fingerprint="sha256:keys", relations=(left, right)))
        self.assertEqual(candidates[0].reason_code, "KEY_TYPE_COMPATIBLE")

    def test_catalog_ids_are_stable_and_constraints_change_schema_fingerprint(self) -> None:
        first = CatalogRelation(
            relation="main.orders",
            columns=(CatalogColumn(name="id", data_type="INTEGER", nullable=False),),
        )
        second = CatalogRelation(
            relation="main.orders",
            columns=(CatalogColumn(name="id", data_type="INTEGER", nullable=False),),
        )
        constrained = first.model_copy(
            update={"keys": (CatalogKey(kind="primary", column_ids=(first.columns[0].column_id,)),)}
        )
        self.assertEqual(first.relation_id, second.relation_id)
        self.assertEqual(first.columns[0].column_id, second.columns[0].column_id)
        self.assertNotEqual(
            catalog_schema_fingerprint((first,)),
            catalog_schema_fingerprint((constrained,)),
        )

    def test_graph_compiler_uses_distinct_role_aliases_for_self_join(self) -> None:
        catalog = CatalogSnapshot(
            schema_fingerprint="sha256:self",
            relations=(
                CatalogRelation(
                    relation="main.employee",
                    columns=(
                        CatalogColumn(name="id", data_type="INTEGER", nullable=False),
                        CatalogColumn(name="manager_id", data_type="INTEGER", nullable=True),
                    ),
                ),
            ),
        )
        relation = catalog.relations[0]
        ids = {column.name: column.column_id for column in relation.columns}
        employee = RelationshipGraphNode(node_id="employee", relation_id=relation.relation_id, role_name="employee", logical_entity="Employee")
        manager = RelationshipGraphNode(node_id="manager", relation_id=relation.relation_id, role_name="manager", logical_entity="Manager")
        edge = RelationshipEdge(
            edge_id="manager_link",
            from_node_id="employee",
            to_node_id="manager",
            conditions=(RelationshipCondition(from_column_id=ids["manager_id"], to_column_id=ids["id"]),),
            cardinality="many_to_one",
            provenance=RelationshipProvenance(source="user"),
        )
        binding = SemanticGraphBindingRecord(
            binding_id="self-graph",
            tenant_id="tenant",
            source_id="source",
            source_snapshot_version=1,
            schema_fingerprint=catalog.schema_fingerprint,
            domain_id="dataset.employee",
            version=1,
            status=SemanticBindingStatus.ACTIVE,
            graph=ActivatedRelationshipGraph(graph_id="self", revision=1, nodes=(employee, manager), edges=(edge,), components=()),
            mappings=(
                SemanticGraphFieldMapping(logical_ref="dataset.Employee.id", node_id="employee", column_id=ids["id"]),
                SemanticGraphFieldMapping(logical_ref="dataset.Manager.id", node_id="manager", column_id=ids["id"]),
            ),
            validation_report_digest="sha256:report",
        )
        prepared = DatasetQueryCompiler().compile(
            plan=DatasetQueryPlan(analysis_type="detail", select=("dataset.Employee.id", "dataset.Manager.id")),
            binding=binding,
            catalog=catalog,
            dialect="sqlite",
            schema_fingerprint=catalog.schema_fingerprint,
            bundle_digest="sha256:bundle",
        )

        self.assertEqual(prepared.allowed_relations, ("main.employee",))
        self.assertIn('AS "node_employee_1"', prepared.executable_sql)
        self.assertIn('AS "node_manager_2"', prepared.executable_sql)

    def test_graph_compiler_preserves_composite_join_conditions(self) -> None:
        catalog = CatalogSnapshot(
            schema_fingerprint="sha256:composite",
            relations=(
                CatalogRelation(relation="main.orders", columns=(CatalogColumn(name="tenant_id", data_type="TEXT", nullable=False), CatalogColumn(name="order_id", data_type="TEXT", nullable=False))),
                CatalogRelation(relation="main.items", columns=(CatalogColumn(name="tenant_id", data_type="TEXT", nullable=False), CatalogColumn(name="order_id", data_type="TEXT", nullable=False))),
            ),
        )
        orders, items = catalog.relations
        order_columns = {column.name: column.column_id for column in orders.columns}
        item_columns = {column.name: column.column_id for column in items.columns}
        graph = ActivatedRelationshipGraph(
            graph_id="composite",
            revision=1,
            nodes=(
                RelationshipGraphNode(node_id="orders", relation_id=orders.relation_id, role_name="orders", logical_entity="Orders"),
                RelationshipGraphNode(node_id="items", relation_id=items.relation_id, role_name="items", logical_entity="Items"),
            ),
            edges=(
                RelationshipEdge(edge_id="order_items", from_node_id="orders", to_node_id="items", conditions=(RelationshipCondition(from_column_id=order_columns["tenant_id"], to_column_id=item_columns["tenant_id"]), RelationshipCondition(from_column_id=order_columns["order_id"], to_column_id=item_columns["order_id"])), cardinality="one_to_many", provenance=RelationshipProvenance(source="user")),
            ),
            components=(),
        )
        binding = SemanticGraphBindingRecord(
            binding_id="composite-graph", tenant_id="tenant", source_id="source", source_snapshot_version=1,
            schema_fingerprint=catalog.schema_fingerprint, domain_id="dataset.orders", version=1, status=SemanticBindingStatus.ACTIVE,
            graph=graph,
            mappings=(
                SemanticGraphFieldMapping(logical_ref="dataset.Orders.id", node_id="orders", column_id=order_columns["order_id"]),
                SemanticGraphFieldMapping(logical_ref="dataset.Items.id", node_id="items", column_id=item_columns["order_id"]),
            ),
            validation_report_digest="sha256:report",
        )
        prepared = DatasetQueryCompiler().compile(
            plan=DatasetQueryPlan(analysis_type="detail", select=("dataset.Orders.id", "dataset.Items.id")),
            binding=binding, catalog=catalog, dialect="sqlite", schema_fingerprint=catalog.schema_fingerprint, bundle_digest="sha256:bundle",
        )

        self.assertIn('"tenant_id" = "node_items_2"."tenant_id"', prepared.executable_sql)
        self.assertIn('AND "node_orders_1"."order_id" = "node_items_2"."order_id"', prepared.executable_sql)
