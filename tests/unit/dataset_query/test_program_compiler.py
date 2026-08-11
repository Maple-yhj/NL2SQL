from __future__ import annotations

import duckdb
import pytest

from data_agent.dataset_query import (
    DatasetAggregateExpression,
    DatasetBinaryExpression,
    DatasetFieldExpression,
    DatasetFunctionExpression,
    DatasetJoinSource,
    DatasetLiteralExpression,
    DatasetMetricExpression,
    DatasetOutputExpression,
    DatasetProjection,
    DatasetQueryProgram,
    DatasetQueryProgramCompiler,
    DatasetQueryStage,
    DatasetRootSource,
    DatasetStageJoinCondition,
    DatasetStageSource,
    DatasetUnionStage,
)
from data_agent.datasources import (
    SemanticBindingRecord,
    SemanticFieldMapping,
    SemanticGraphBindingRecord,
    SemanticGraphFieldMapping,
    SemanticMetricDefinition,
)
from data_agent.relationships.models import (
    ActivatedRelationshipGraph,
    RelationshipGraphNode,
)
from data_agent.tools.schemas import CatalogColumn, CatalogRelation, CatalogSnapshot


FINGERPRINT = "sha256:generic-program-compiler"


def _field(name: str) -> DatasetFieldExpression:
    return DatasetFieldExpression(ref=f"dataset.activity.{name}")


def _output(stage_id: str, name: str) -> DatasetOutputExpression:
    return DatasetOutputExpression(stage_id=stage_id, name=name)


def _literal(value: str | int) -> DatasetLiteralExpression:
    return DatasetLiteralExpression(value=value)


def _eq(left, right) -> DatasetBinaryExpression:
    return DatasetBinaryExpression(operation="eq", left=left, right=right)


def _binding() -> SemanticBindingRecord:
    return SemanticBindingRecord(
        binding_id="generic-binding",
        tenant_id="tenant",
        source_id="source",
        source_snapshot_version=1,
        domain_id="generic-domain",
        version=1,
        status="active",
        mappings=tuple(
            SemanticFieldMapping(
                logical_ref=f"dataset.activity.{name}",
                physical_relation="main.activity",
                physical_column=name,
            )
            for name in ("entity_id", "occurred_at", "state", "amount")
        ),
        metrics=(
            SemanticMetricDefinition(
                metric_ref="metric.activity_value",
                display_name="Activity value",
                description="Sum of governed activity amounts",
                operation="sum",
                field_ref="dataset.activity.amount",
                unit="currency",
                synonyms=("total value",),
            ),
        ),
    )


def _catalog() -> CatalogSnapshot:
    return CatalogSnapshot(
        schema_fingerprint=FINGERPRINT,
        relations=(
            CatalogRelation(
                relation="main.activity",
                columns=(
                    CatalogColumn(name="entity_id", data_type="VARCHAR", nullable=False),
                    CatalogColumn(name="occurred_at", data_type="TIMESTAMP", nullable=False),
                    CatalogColumn(name="state", data_type="VARCHAR", nullable=False),
                    CatalogColumn(name="amount", data_type="DOUBLE", nullable=False),
                ),
            ),
        ),
    )


def _compile(program: DatasetQueryProgram, dialect: str = "duckdb"):
    return DatasetQueryProgramCompiler().compile(
        program=program,
        binding=_binding(),
        dialect=dialect,
        schema_fingerprint=FINGERPRINT,
        bundle_digest="bundle",
        catalog=_catalog(),
    )


def test_time_bucket_is_lowered_per_dialect_and_ctes_are_not_physical_relations() -> None:
    month = DatasetFunctionExpression(
        operation="time_bucket",
        time_grain="month",
        arguments=(_field("occurred_at"),),
    )
    program = DatasetQueryProgram(
        stages=(
            DatasetQueryStage(
                stage_id="monthly",
                input=DatasetRootSource(),
                projections=(
                    DatasetProjection(alias="month", expression=month),
                    DatasetProjection(
                        alias="event_count",
                        expression=DatasetAggregateExpression(operation="count"),
                    ),
                ),
                group_by=(month,),
            ),
        ),
        output_stage_id="monthly",
    )

    sqlite = _compile(program, "sqlite")
    postgres = _compile(program, "postgres")

    assert "STRFTIME" in sqlite.executable_sql
    assert "DATE_TRUNC" in postgres.executable_sql
    assert sqlite.allowed_relations == ("main.activity",)
    assert "monthly" not in sqlite.allowed_relations


def test_date_part_supports_hour_and_weekday_without_raw_sql() -> None:
    hour = DatasetFunctionExpression(
        operation="date_part",
        date_part="hour",
        arguments=(_field("occurred_at"),),
    )
    program = DatasetQueryProgram(
        stages=(
            DatasetQueryStage(
                stage_id="hourly",
                input=DatasetRootSource(),
                projections=(
                    DatasetProjection(alias="hour", expression=hour),
                    DatasetProjection(
                        alias="event_count",
                        expression=DatasetAggregateExpression(operation="count"),
                    ),
                ),
                group_by=(hour,),
            ),
        ),
        output_stage_id="hourly",
    )

    assert "%H" in _compile(program, "sqlite").executable_sql
    assert "EXTRACT(HOUR" in _compile(program, "postgres").executable_sql


def test_nested_conditional_aggregation_executes_as_one_readonly_query() -> None:
    program = DatasetQueryProgram(
        stages=(
            DatasetQueryStage(
                stage_id="by_entity",
                input=DatasetRootSource(),
                projections=(
                    DatasetProjection(alias="entity_id", expression=_field("entity_id")),
                    DatasetProjection(
                        alias="event_count",
                        expression=DatasetAggregateExpression(operation="count"),
                    ),
                    DatasetProjection(
                        alias="completed_count",
                        expression=DatasetAggregateExpression(
                            operation="count",
                            filter=_eq(_field("state"), _literal("completed")),
                        ),
                    ),
                    DatasetProjection(
                        alias="amount_total",
                        expression=DatasetAggregateExpression(
                            operation="sum", operand=_field("amount")
                        ),
                    ),
                ),
                group_by=(_field("entity_id"),),
            ),
            DatasetQueryStage(
                stage_id="distribution",
                input=DatasetStageSource(stage_id="by_entity"),
                projections=(
                    DatasetProjection(
                        alias="entity_count",
                        expression=DatasetAggregateExpression(operation="count"),
                    ),
                    DatasetProjection(
                        alias="fully_completed_count",
                        expression=DatasetAggregateExpression(
                            operation="count",
                            filter=_eq(
                                _output("by_entity", "completed_count"),
                                _output("by_entity", "event_count"),
                            ),
                        ),
                    ),
                    DatasetProjection(
                        alias="maximum_amount",
                        expression=DatasetAggregateExpression(
                            operation="max",
                            operand=_output("by_entity", "amount_total"),
                        ),
                    ),
                ),
            ),
        ),
        output_stage_id="distribution",
    )
    prepared = _compile(program)

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE activity "
            "(entity_id VARCHAR, occurred_at TIMESTAMP, state VARCHAR, amount DOUBLE)"
        )
        connection.executemany(
            "INSERT INTO activity VALUES (?, ?, ?, ?)",
            (
                ("a", "2025-01-01", "completed", 10.0),
                ("a", "2025-01-02", "completed", 20.0),
                ("b", "2025-02-01", "completed", 7.0),
                ("b", "2025-02-02", "pending", 3.0),
            ),
        )
        row = connection.execute(
            prepared.executable_sql,
            [parameter.value for parameter in prepared.parameters],
        ).fetchone()
    finally:
        connection.close()

    assert row == (2, 1, 30.0)
    assert prepared.allowed_relations == ("main.activity",)
    assert prepared.executable_sql.lstrip().startswith("WITH")


def test_median_and_case_insensitive_contains_execute_without_raw_sql() -> None:
    contains_completed = DatasetFunctionExpression(
        operation="contains_ci",
        arguments=(_field("state"), _literal("PLET")),
    )
    program = DatasetQueryProgram(
        stages=(
            DatasetQueryStage(
                stage_id="robust_summary",
                input=DatasetRootSource(),
                projections=(
                    DatasetProjection(
                        alias="median_amount",
                        expression=DatasetAggregateExpression(
                            operation="median",
                            operand=_field("amount"),
                        ),
                    ),
                    DatasetProjection(
                        alias="matching_rows",
                        expression=DatasetAggregateExpression(
                            operation="count",
                            filter=contains_completed,
                        ),
                    ),
                ),
            ),
        ),
        output_stage_id="robust_summary",
    )
    prepared = _compile(program)

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE activity "
            "(entity_id VARCHAR, occurred_at TIMESTAMP, state VARCHAR, amount DOUBLE)"
        )
        connection.executemany(
            "INSERT INTO activity VALUES (?, ?, ?, ?)",
            (
                ("a", "2025-01-01", "completed", 10.0),
                ("a", "2025-01-02", "COMPLETED", 20.0),
                ("b", "2025-02-01", "completed", 7.0),
                ("b", "2025-02-02", "pending", 3.0),
            ),
        )
        row = connection.execute(
            prepared.executable_sql,
            [parameter.value for parameter in prepared.parameters],
        ).fetchone()
    finally:
        connection.close()

    assert row == (8.5, 3)


def test_calendar_month_difference_is_lowered_for_all_dialects() -> None:
    month_difference = DatasetFunctionExpression(
        operation="date_diff_months",
        arguments=(_field("occurred_at"), _literal("2025-04-01")),
    )
    program = DatasetQueryProgram(
        stages=(
            DatasetQueryStage(
                stage_id="calendar_offset",
                input=DatasetRootSource(),
                projections=(
                    DatasetProjection(alias="month_offset", expression=month_difference),
                ),
            ),
        ),
        output_stage_id="calendar_offset",
    )

    assert "STRFTIME" in _compile(program, "sqlite").executable_sql
    prepared = _compile(program, "duckdb")
    assert "EXTRACT(YEAR" in _compile(program, "postgres").executable_sql

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE activity "
            "(entity_id VARCHAR, occurred_at TIMESTAMP, state VARCHAR, amount DOUBLE)"
        )
        connection.execute(
            "INSERT INTO activity VALUES ('a', '2025-01-15', 'ready', 1.0)"
        )
        row = connection.execute(
            prepared.executable_sql,
            [parameter.value for parameter in prepared.parameters],
        ).fetchone()
    finally:
        connection.close()

    assert row == (3,)


def test_power_is_available_for_dataset_neutral_distance_formulas() -> None:
    squared = DatasetFunctionExpression(
        operation="power",
        arguments=(_field("amount"), _literal(2)),
    )
    program = DatasetQueryProgram(
        stages=(
            DatasetQueryStage(
                stage_id="squared_values",
                input=DatasetRootSource(),
                projections=(DatasetProjection(alias="value_squared", expression=squared),),
                limit=1,
            ),
        ),
        output_stage_id="squared_values",
    )

    assert "POWER" in _compile(program, "duckdb").executable_sql
    assert "POWER" in _compile(program, "postgres").executable_sql


def test_nested_arithmetic_preserves_ast_parentheses_during_execution() -> None:
    add_then_multiply = DatasetBinaryExpression(
        operation="multiply",
        left=DatasetBinaryExpression(
            operation="add",
            left=_field("amount"),
            right=_literal(2),
        ),
        right=_literal(3),
    )
    subtract_group = DatasetBinaryExpression(
        operation="subtract",
        left=_literal(100),
        right=DatasetBinaryExpression(
            operation="add",
            left=_field("amount"),
            right=_literal(1),
        ),
    )
    prepared = _compile(
        DatasetQueryProgram(
            stages=(
                DatasetQueryStage(
                    stage_id="expression_values",
                    input=DatasetRootSource(),
                    projections=(
                        DatasetProjection(
                            alias="grouped_product",
                            expression=add_then_multiply,
                        ),
                        DatasetProjection(
                            alias="grouped_subtraction",
                            expression=subtract_group,
                        ),
                    ),
                    limit=1,
                ),
            ),
            output_stage_id="expression_values",
        )
    )

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE activity "
            "(entity_id VARCHAR, occurred_at TIMESTAMP, state VARCHAR, amount DOUBLE)"
        )
        connection.execute(
            "INSERT INTO activity VALUES ('a', '2025-01-01', 'ready', 10.0)"
        )
        row = connection.execute(
            prepared.executable_sql,
            [parameter.value for parameter in prepared.parameters],
        ).fetchone()
    finally:
        connection.close()

    assert row == (36.0, 89.0)


def test_explicit_semantic_metric_is_lowered_without_name_guessing() -> None:
    prepared = _compile(
        DatasetQueryProgram(
            stages=(
                DatasetQueryStage(
                    stage_id="metric_summary",
                    input=DatasetRootSource(),
                    projections=(
                        DatasetProjection(
                            alias="governed_value",
                            expression=DatasetMetricExpression(
                                ref="metric.activity_value"
                            ),
                        ),
                    ),
                ),
            ),
            output_stage_id="metric_summary",
        )
    )

    assert "SUM" in prepared.executable_sql
    assert '"amount"' in prepared.executable_sql
    assert prepared.allowed_relations == ("main.activity",)


def test_stage_join_and_union_are_compiled_without_dataset_specific_rules() -> None:
    by_entity = DatasetQueryStage(
        stage_id="entity_totals",
        input=DatasetRootSource(),
        projections=(
            DatasetProjection(alias="entity_id", expression=_field("entity_id")),
            DatasetProjection(
                alias="value",
                expression=DatasetAggregateExpression(
                    operation="sum", operand=_field("amount")
                ),
            ),
        ),
        group_by=(_field("entity_id"),),
    )
    completed = DatasetQueryStage(
        stage_id="completed_totals",
        input=DatasetRootSource(),
        projections=(
            DatasetProjection(alias="entity_id", expression=_field("entity_id")),
            DatasetProjection(
                alias="value",
                expression=DatasetAggregateExpression(
                    operation="sum",
                    operand=_field("amount"),
                    filter=_eq(_field("state"), _literal("completed")),
                ),
            ),
        ),
        group_by=(_field("entity_id"),),
    )
    joined = DatasetQueryStage(
        stage_id="joined",
        input=DatasetJoinSource(
            left_stage_id="entity_totals",
            right_stage_id="completed_totals",
            conditions=(
                DatasetStageJoinCondition(
                    left_name="entity_id", right_name="entity_id"
                ),
            ),
        ),
        projections=(
            DatasetProjection(
                alias="entity_id",
                expression=_output("entity_totals", "entity_id"),
            ),
            DatasetProjection(
                alias="value",
                expression=DatasetBinaryExpression(
                    operation="subtract",
                    left=_output("entity_totals", "value"),
                    right=_output("completed_totals", "value"),
                ),
            ),
        ),
    )
    union = DatasetUnionStage(
        stage_id="combined",
        input_stage_ids=("entity_totals", "joined"),
    )
    prepared = _compile(
        DatasetQueryProgram(
            stages=(by_entity, completed, joined, union),
            output_stage_id="combined",
        )
    )

    assert "INNER JOIN" in prepared.executable_sql
    assert "UNION ALL" in prepared.executable_sql
    assert prepared.allowed_relations == ("main.activity",)


def test_stage_join_cannot_bypass_a_disconnected_active_relationship_graph() -> None:
    catalog = CatalogSnapshot(
        schema_fingerprint="sha256:disconnected-program",
        relations=(
            CatalogRelation(
                relation="main.left_events",
                columns=(
                    CatalogColumn(name="entity_id", data_type="VARCHAR", nullable=False),
                ),
            ),
            CatalogRelation(
                relation="main.right_events",
                columns=(
                    CatalogColumn(name="entity_id", data_type="VARCHAR", nullable=False),
                ),
            ),
        ),
    )
    left_relation, right_relation = catalog.relations
    binding = SemanticGraphBindingRecord(
        binding_id="disconnected-binding",
        tenant_id="tenant",
        source_id="source",
        source_snapshot_version=1,
        schema_fingerprint=catalog.schema_fingerprint,
        domain_id="generic",
        version=1,
        status="active",
        graph=ActivatedRelationshipGraph(
            graph_id="disconnected",
            revision=1,
            nodes=(
                RelationshipGraphNode(
                    node_id="left",
                    relation_id=left_relation.relation_id,
                    role_name="left",
                    logical_entity="LeftEvent",
                ),
                RelationshipGraphNode(
                    node_id="right",
                    relation_id=right_relation.relation_id,
                    role_name="right",
                    logical_entity="RightEvent",
                ),
            ),
            edges=(),
            components=(),
        ),
        mappings=(
            SemanticGraphFieldMapping(
                logical_ref="generic.Left.entity_id",
                node_id="left",
                column_id=left_relation.columns[0].column_id,
            ),
            SemanticGraphFieldMapping(
                logical_ref="generic.Right.entity_id",
                node_id="right",
                column_id=right_relation.columns[0].column_id,
            ),
        ),
        validation_report_digest="sha256:disconnected-report",
    )

    def root(stage_id: str, ref: str) -> DatasetQueryStage:
        return DatasetQueryStage(
            stage_id=stage_id,
            input=DatasetRootSource(),
            projections=(
                DatasetProjection(alias="entity_id", expression=DatasetFieldExpression(ref=ref)),
            ),
        )

    program = DatasetQueryProgram(
        stages=(
            root("left_stage", "generic.Left.entity_id"),
            root("right_stage", "generic.Right.entity_id"),
            DatasetQueryStage(
                stage_id="joined",
                input=DatasetJoinSource(
                    left_stage_id="left_stage",
                    right_stage_id="right_stage",
                    conditions=(
                        DatasetStageJoinCondition(
                            left_name="entity_id",
                            right_name="entity_id",
                        ),
                    ),
                ),
                projections=(
                    DatasetProjection(
                        alias="entity_id",
                        expression=DatasetOutputExpression(
                            stage_id="left_stage", name="entity_id"
                        ),
                    ),
                ),
            ),
        ),
        output_stage_id="joined",
    )

    with pytest.raises(ValueError, match="cannot bypass"):
        DatasetQueryProgramCompiler().compile(
            program=program,
            binding=binding,
            dialect="sqlite",
            schema_fingerprint=catalog.schema_fingerprint,
            bundle_digest="bundle",
            catalog=catalog,
        )
