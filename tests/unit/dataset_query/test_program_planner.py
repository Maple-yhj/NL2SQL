from __future__ import annotations

import asyncio
import json

import pytest

from data_agent.dataset_query import (
    DatasetAggregateExpression,
    DatasetFieldExpression,
    DatasetOutputExpression,
    DatasetProjection,
    DatasetQueryProgram,
    DatasetQueryProgramPlanner,
    DatasetQueryStage,
    DatasetRootSource,
    DatasetStageSource,
)
from data_agent.datasources import (
    SemanticBindingRecord,
    SemanticFieldMapping,
    SemanticMetricDefinition,
)
from data_agent.tools.schemas import CatalogColumn, CatalogRelation, CatalogSnapshot


class _SequenceModel:
    model_id = "program-planner-test"
    version = "1"

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = [json.dumps(item) for item in responses]
        self.prompts: list[str] = []

    async def complete(self, prompt: str, **_: object) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


def _catalog_and_binding() -> tuple[CatalogSnapshot, SemanticBindingRecord]:
    catalog = CatalogSnapshot(
        schema_fingerprint="sha256:program-planner",
        relations=(
            CatalogRelation(
                relation="main.events",
                columns=(
                    CatalogColumn(name="actor", data_type="VARCHAR", nullable=False),
                    CatalogColumn(name="value", data_type="DOUBLE", nullable=False),
                ),
            ),
        ),
    )
    binding = SemanticBindingRecord(
        binding_id="program-planner-binding",
        tenant_id="tenant",
        source_id="source",
        source_snapshot_version=1,
        domain_id="generic",
        version=1,
        status="active",
        mappings=(
            SemanticFieldMapping(
                logical_ref="generic.Event.actor",
                physical_relation="main.events",
                physical_column="actor",
            ),
            SemanticFieldMapping(
                logical_ref="generic.Event.value",
                physical_relation="main.events",
                physical_column="value",
                display_name="Event value",
                description="Value recorded when the event occurred",
                semantic_role="measure",
                entity="Event",
                grain="one row per event",
                unit="points",
                lifecycle_stage="event occurrence",
                synonyms=("activity value",),
            ),
        ),
        metrics=(
            SemanticMetricDefinition(
                metric_ref="metric.total_event_value",
                display_name="Total event value",
                description="Sum of event values",
                operation="sum",
                field_ref="generic.Event.value",
                unit="points",
                synonyms=("total activity value",),
            ),
        ),
    )
    return catalog, binding


def _program(measure_ref: str = "generic.Event.value") -> DatasetQueryProgram:
    return DatasetQueryProgram(
        stages=(
            DatasetQueryStage(
                stage_id="per_actor",
                input=DatasetRootSource(),
                projections=(
                    DatasetProjection(
                        alias="actor",
                        expression=DatasetFieldExpression(ref="generic.Event.actor"),
                    ),
                    DatasetProjection(
                        alias="total_value",
                        expression=DatasetAggregateExpression(
                            operation="sum",
                            operand=DatasetFieldExpression(ref=measure_ref),
                        ),
                    ),
                ),
                group_by=(DatasetFieldExpression(ref="generic.Event.actor"),),
            ),
            DatasetQueryStage(
                stage_id="summary",
                input=DatasetStageSource(stage_id="per_actor"),
                projections=(
                    DatasetProjection(
                        alias="actor_count",
                        expression=DatasetAggregateExpression(operation="count"),
                    ),
                    DatasetProjection(
                        alias="maximum_value",
                        expression=DatasetAggregateExpression(
                            operation="max",
                            operand=DatasetOutputExpression(
                                stage_id="per_actor", name="total_value"
                            ),
                        ),
                    ),
                ),
            ),
        ),
        output_stage_id="summary",
    )


def test_program_planner_repairs_unknown_refs_and_accepts_nested_aggregation() -> None:
    catalog, binding = _catalog_and_binding()
    model = _SequenceModel(
        [
            _program("generic.Event.unknown").model_dump(mode="json"),
            _program().model_dump(mode="json"),
        ]
    )

    result = asyncio.run(
        DatasetQueryProgramPlanner(model).build_program(
            question="Summarize totals per actor, then return the maximum",
            binding=binding,
            catalog=catalog,
        )
    )

    assert result.program.output_stage_id == "summary"
    assert len(model.prompts) == 2
    first = json.loads(model.prompts[0])
    second = json.loads(model.prompts[1])
    assert first["task"] == "create_dataset_query_program"
    assert "datasetQueryProgramSchema" in first
    assert "main.events" not in model.prompts[0]
    assert first["logicalMetrics"][0]["ref"] == "metric.total_event_value"
    value_field = next(
        item for item in first["logicalCatalog"] if item["ref"] == "generic.Event.value"
    )
    assert value_field["grain"] == "one row per event"
    assert value_field["lifecycleStage"] == "event occurrence"
    assert second["task"] == "repair_dataset_query_program"
    assert "outside logicalCatalog" in second["validationErrors"]


def test_undefined_accounting_metric_fails_closed_before_model_planning() -> None:
    catalog, binding = _catalog_and_binding()
    model = _SequenceModel([])

    result = asyncio.run(
        DatasetQueryProgramPlanner(model).build_program(
            question="What is the company's total revenue?",
            binding=binding,
            catalog=catalog,
        )
    )

    assert result.program.status == "unsupported"
    assert "governed semantic definition" in (
        result.program.clarification_question or ""
    )
    assert model.prompts == []


def test_high_risk_policy_uses_only_the_current_turn() -> None:
    catalog, binding = _catalog_and_binding()
    model = _SequenceModel([_program().model_dump(mode="json")])

    result = asyncio.run(
        DatasetQueryProgramPlanner(model).build_program(
            question="Earlier context mentioned revenue; now summarize event values",
            policy_question="Now summarize event values",
            binding=binding,
            catalog=catalog,
        )
    )

    assert result.program.status == "ready"
    assert len(model.prompts) == 1


def test_query_planning_model_has_a_finite_stage_timeout() -> None:
    catalog, binding = _catalog_and_binding()

    class _SlowModel:
        model_id = "slow"
        version = "1"
        cancelled = False

        async def complete(self, *_: object, **__: object) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                # Simulate a provider that suppresses cancellation while it
                # finishes response cleanup.
                return json.dumps(_program().model_dump(mode="json"))

    model = _SlowModel()
    with pytest.raises(TimeoutError, match="timed out"):
        asyncio.run(
            DatasetQueryProgramPlanner(
                model,
                max_attempts=1,
                model_timeout_seconds=0.01,
            ).build_program(
                question="Summarize event values",
                binding=binding,
                catalog=catalog,
            )
        )

    assert model.cancelled
