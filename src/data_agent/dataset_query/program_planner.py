"""Model-assisted planning into the dataset-neutral query-program IR."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Protocol

from data_agent.datasources import SemanticBindingRecord, SemanticGraphBindingRecord
from data_agent.relationships.router import (
    GraphRouteError,
    GraphRouteRequest,
    GraphRouteResolver,
)
from data_agent.tools.schemas import CatalogSnapshot
from data_agent.semantic_metrics import (
    DomainPackRegistry,
    EffectiveMetricCatalog,
    LegacyMetricAdapter,
    MetricCatalogEntry,
    MetricCatalogOrigin,
    SemanticMetricDefinitionV2,
)

from .models import DatasetPlanStatus
from .program import (
    DatasetAggregateExpression,
    DatasetBinaryExpression,
    DatasetFieldExpression,
    DatasetFunctionExpression,
    DatasetJoinSource,
    DatasetLiteralExpression,
    DatasetMetricExpression,
    DatasetOutputExpression,
    DatasetQueryProgram,
    DatasetQueryStage,
    DatasetRootSource,
    DatasetScalarExpression,
    DatasetStageSource,
    DatasetUnaryExpression,
    DatasetUnionStage,
)


class ModelClient(Protocol):
    model_id: str
    version: str

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 4096,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class DatasetProgramPlanningResult:
    program: DatasetQueryProgram
    contextualized_question: str


_SYSTEM_PROMPT = (
    "You are a governed, dataset-neutral analytical query planner. Return exactly "
    "one JSON object matching datasetQueryProgramSchema. Never return SQL or "
    "physical relation/column names. Use field refs from logicalCatalog and metric "
    "refs from logicalMetrics. Prefer kind=metric whenever a requested business "
    "metric has an explicit definition. Build a "
    "finite topologically ordered stage DAG for multi-step questions: root stages "
    "read logical fields, later stages consume named outputs, joins combine prior "
    "stages, and union_all combines identical ordered outputs. Use time_bucket for "
    "calendar grouping, aggregate filters for conditional aggregation, and later "
    "stages for nested aggregation. "
    "Every dimension and measure explicitly requested by the user must appear as "
    "a final output projection; do not omit requested descriptive dimensions or "
    "auxiliary counts. For every rate or ratio, also project its auditable "
    "numerator and denominator, plus an excluded-row count when exclusions apply. "
    "Use median aggregates for exact numeric medians, contains_ci for "
    "case-insensitive substring predicates, and date_diff_months for calendar "
    "cohort offsets. Sequential-event questions may use two independently "
    "projected root stages, an equality stage join, ordered-pair filters, and a "
    "nested minimum to identify each next event. Pearson correlation may be "
    "derived from count, sum(x), sum(y), sum(x*y), sum(x*x), and sum(y*y). "
    "Use power together with radians, sin, cos, sqrt, and asin for governed "
    "great-circle distance calculations when the requested fields exist. "
    "A capability gap is unsupported; ask for "
    "clarification only when a user-provided definition or choice would make the "
    "request executable. Never substitute an undefined business metric with a "
    "similarly named field. Prefer the smallest sufficient program."
)
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL | re.IGNORECASE)
def _consume_background_task_result(task: asyncio.Task[str]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


class DatasetQueryProgramPlanner:
    def __init__(
        self,
        model_client: ModelClient,
        *,
        max_attempts: int = 3,
        model_timeout_seconds: float = 60.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if model_timeout_seconds <= 0:
            raise ValueError("model timeout must be positive")
        self._model_client = model_client
        self._max_attempts = max_attempts
        self._model_timeout_seconds = model_timeout_seconds

    async def build_program(
        self,
        *,
        question: str,
        policy_question: str | None = None,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        catalog: CatalogSnapshot,
        clarification_history: tuple[dict[str, str], ...] = (),
        metric_catalog: EffectiveMetricCatalog | None = None,
        domain_packs: DomainPackRegistry | None = None,
    ) -> DatasetProgramPlanningResult:
        metric_catalog = metric_catalog or self._legacy_metric_catalog(binding)
        metric_definitions = {
            item.definition.metric_ref: item.definition
            for item in metric_catalog.entries
        }
        logical_catalog = self._logical_catalog(binding=binding, catalog=catalog)
        logical_metrics = [
            {
                "ref": item.metric_ref,
                "displayName": item.display_name,
                "description": item.description,
                "formula": item.formula.model_dump(mode="json"),
                "defaultFilter": (
                    item.default_filter.model_dump(mode="json")
                    if item.default_filter is not None
                    else None
                ),
                "defaultTimeRef": item.default_time_ref,
                "unit": item.unit,
                "grain": item.grain,
                "currency": item.currency,
                "synonyms": item.synonyms,
            }
            for item in metric_definitions.values()
        ]
        policy_input = policy_question or question
        unresolved_templates = tuple(
            item
            for item in (domain_packs or DomainPackRegistry()).detect_templates(
                policy_input,
            )
            if item[1].metric_ref not in metric_definitions
        )
        if unresolved_templates:
            manifest, template, matched_term = unresolved_templates[0]
            return DatasetProgramPlanningResult(
                program=DatasetQueryProgram(
                    status=(
                        DatasetPlanStatus.NEEDS_CLARIFICATION
                        if manifest.domain_id == binding.domain_id
                        else DatasetPlanStatus.UNSUPPORTED
                    ),
                    clarification_question=(
                        "The requested business metric has no governed semantic "
                        f"definition in this dataset（检测到“{matched_term}”）。"
                        "请先在指标候选中确认金额基数、状态范围、时间字段、"
                        "退款处理和币种；确认后的临时口径可用于本次查询，"
                        f"管理员批准后会发布为 {template.metric_ref}。"
                    ),
                ),
                contextualized_question=question,
            )
        request = {
            "task": "create_dataset_query_program",
            "question": question,
            "clarificationHistory": clarification_history,
            "logicalCatalog": logical_catalog,
            "logicalMetrics": logical_metrics,
            "datasetQueryProgramSchema": DatasetQueryProgram.model_json_schema(),
        }
        prompt = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        failure = ""
        timed_out = False
        for attempt in range(self._max_attempts):
            try:
                raw = await self._complete_before_timeout(
                    prompt,
                    system=_SYSTEM_PROMPT,
                )
            except TimeoutError:
                failure = "query planning model timed out"
                timed_out = True
                break
            try:
                program = DatasetQueryProgram.model_validate(self._json_object(raw))
                self._validate_program_scope(
                    program=program,
                    logical_refs=tuple(item["ref"] for item in logical_catalog),
                    metric_refs=tuple(item["ref"] for item in logical_metrics),
                    binding=binding,
                    metric_definitions=metric_definitions,
                )
                return DatasetProgramPlanningResult(
                    program=program,
                    contextualized_question=question,
                )
            except ValueError as exc:
                failure = str(exc)
                if attempt + 1 >= self._max_attempts:
                    break
                prompt = json.dumps(
                    {
                        "task": "repair_dataset_query_program",
                        "input": request,
                        "previousResponse": raw[:8000],
                        "validationErrors": failure[:4000],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
        if timed_out:
            raise TimeoutError(failure)
        raise ValueError("model failed to produce a valid DatasetQueryProgram: " + failure)

    async def _complete_before_timeout(self, prompt: str, *, system: str) -> str:
        """Bound a provider call even when its cancellation cleanup is uncooperative."""

        task = asyncio.create_task(
            self._model_client.complete(
                prompt,
                system=system,
                max_output_tokens=4096,
            )
        )
        done, _ = await asyncio.wait({task}, timeout=self._model_timeout_seconds)
        if task in done:
            return task.result()
        task.cancel()
        # Do not await cancellation here. Some HTTP/provider clients defer or
        # suppress CancelledError while closing a response, which previously let
        # one planning call consume the entire analysis deadline. The callback
        # still retrieves any eventual exception so the orphaned task is quiet.
        task.add_done_callback(_consume_background_task_result)
        raise TimeoutError("query planning model timed out")

    @staticmethod
    def _logical_catalog(
        *,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        catalog: CatalogSnapshot,
    ) -> list[dict[str, object]]:
        type_by_physical = {
            (relation.relation, column.name): column.data_type
            for relation in catalog.relations
            for column in relation.columns
        }
        if isinstance(binding, SemanticBindingRecord):
            return [
                {
                    "ref": mapping.logical_ref,
                    "type": type_by_physical.get(
                        (mapping.physical_relation, mapping.physical_column),
                        "unknown",
                    ),
                    **DatasetQueryProgramPlanner._field_metadata(mapping),
                }
                for mapping in binding.mappings
            ]
        relation_by_node = {
            node.node_id: node.relation_id for node in binding.graph.nodes
        }
        relation_by_id = {
            relation.relation_id: relation.relation for relation in catalog.relations
        }
        column_by_id = {
            column.column_id: (relation.relation, column.name)
            for relation in catalog.relations
            for column in relation.columns
        }
        return [
            {
                "ref": mapping.logical_ref,
                "type": type_by_physical.get(
                    column_by_id.get(mapping.column_id, ("", "")),
                    "unknown",
                ),
                "nodeId": mapping.node_id,
                **DatasetQueryProgramPlanner._field_metadata(mapping),
            }
            for mapping in binding.mappings
            if relation_by_node.get(mapping.node_id) in relation_by_id
        ]

    @staticmethod
    def _field_metadata(mapping) -> dict[str, object]:
        values = {
            "displayName": mapping.display_name,
            "description": mapping.description,
            "semanticRole": mapping.semantic_role,
            "entity": mapping.entity,
            "grain": mapping.grain,
            "unit": mapping.unit,
            "lifecycleStage": mapping.lifecycle_stage,
            "synonyms": mapping.synonyms,
        }
        return {key: value for key, value in values.items() if value not in (None, ())}

    @staticmethod
    def _metric_matches_question(question: str, metric) -> bool:
        lowered = question.casefold()
        candidates = (
            metric.metric_ref,
            metric.metric_ref.rsplit(".", 1)[-1],
            metric.display_name,
            *metric.synonyms,
        )
        return any(item.casefold() in lowered for item in candidates)

    @classmethod
    def _validate_program_scope(
        cls,
        *,
        program: DatasetQueryProgram,
        logical_refs: tuple[str, ...],
        metric_refs: tuple[str, ...],
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        metric_definitions: dict[str, SemanticMetricDefinitionV2],
    ) -> None:
        if program.status != DatasetPlanStatus.READY:
            return
        allowed = set(logical_refs)
        outputs: dict[str, set[str]] = {}
        for stage in program.stages:
            if isinstance(stage, DatasetUnionStage):
                expected = outputs[stage.input_stage_ids[0]]
                if any(outputs[item] != expected for item in stage.input_stage_ids[1:]):
                    raise ValueError("union inputs must expose identical output names")
                outputs[stage.stage_id] = set(expected)
                continue
            refs = cls._query_stage_field_refs(stage)
            metrics = cls._query_stage_metric_refs(stage)
            unknown = set(refs) - allowed
            if unknown:
                raise ValueError(
                    "query program references logical refs outside logicalCatalog: "
                    + ", ".join(sorted(unknown))
                )
            unknown_metrics = set(metrics) - set(metric_refs)
            if unknown_metrics:
                raise ValueError(
                    "query program references metrics outside logicalMetrics: "
                    + ", ".join(sorted(unknown_metrics))
                )
            cls._validate_output_refs(stage=stage, outputs=outputs)
            if isinstance(stage.input, DatasetRootSource):
                if stage.input.anchor_ref is not None and stage.input.anchor_ref not in allowed:
                    raise ValueError("root stage anchor_ref is outside logicalCatalog")
                if (
                    stage.input.anchor_ref is None
                    and not refs
                    and not any(metric_definitions[ref].ast_field_refs for ref in metrics)
                ):
                    raise ValueError(
                        "root stage count metrics without a field require anchor_ref"
                    )
                if isinstance(binding, SemanticGraphBindingRecord):
                    mapping_by_ref = {item.logical_ref: item for item in binding.mappings}
                    routed_refs = tuple(
                        dict.fromkeys(
                            ((stage.input.anchor_ref,) if stage.input.anchor_ref else ())
                            + refs
                            + tuple(
                                field_ref
                                for ref in metrics
                                for field_ref in metric_definitions[ref].ast_field_refs
                            )
                        )
                    )
                    nodes = tuple(
                        dict.fromkeys(mapping_by_ref[ref].node_id for ref in routed_refs)
                    )
                    try:
                        GraphRouteResolver().resolve(
                            binding.graph,
                            GraphRouteRequest(
                                required_node_ids=nodes,
                                required_logical_refs=routed_refs,
                            ),
                        )
                    except GraphRouteError as exc:
                        raise ValueError(
                            "root stage logical refs do not share a safe active "
                            f"relationship route ({exc.code})"
                        ) from exc
            outputs[stage.stage_id] = {item.alias for item in stage.projections}

    @staticmethod
    def _legacy_metric_catalog(
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
    ) -> EffectiveMetricCatalog:
        return EffectiveMetricCatalog.build(
            legacy=tuple(
                MetricCatalogEntry.create(
                    definition=LegacyMetricAdapter.to_v2(metric),
                    origin=MetricCatalogOrigin.LEGACY,
                    authority_ref=(
                        f"embedded-v1:{binding.binding_id}:{binding.version}"
                    ),
                )
                for metric in binding.metrics
            )
        )

    @classmethod
    def _validate_output_refs(
        cls,
        *,
        stage: DatasetQueryStage,
        outputs: dict[str, set[str]],
    ) -> None:
        if isinstance(stage.input, DatasetRootSource):
            available_stages: set[str] = set()
        elif isinstance(stage.input, DatasetStageSource):
            available_stages = {stage.input.stage_id}
        else:
            assert isinstance(stage.input, DatasetJoinSource)
            available_stages = {
                stage.input.left_stage_id,
                stage.input.right_stage_id,
            }
            for condition in stage.input.conditions:
                if condition.left_name not in outputs[stage.input.left_stage_id]:
                    raise ValueError("stage join references an unknown left output")
                if condition.right_name not in outputs[stage.input.right_stage_id]:
                    raise ValueError("stage join references an unknown right output")
        for expression in cls._query_stage_scalars(stage):
            for output in cls._scalar_output_refs(expression):
                if output.stage_id not in available_stages:
                    raise ValueError(
                        f"stage output {output.stage_id}.{output.name} is unavailable"
                    )
                if output.name not in outputs[output.stage_id]:
                    raise ValueError(
                        f"stage output {output.stage_id}.{output.name} is unknown"
                    )

    @classmethod
    def _query_stage_field_refs(cls, stage: DatasetQueryStage) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                ref
                for expression in cls._query_stage_scalars(stage)
                for ref in cls._scalar_field_refs(expression)
            )
        )

    @staticmethod
    def _query_stage_scalars(stage: DatasetQueryStage) -> tuple[DatasetScalarExpression, ...]:
        values: list[DatasetScalarExpression] = []
        for projection in stage.projections:
            expression = projection.expression
            if isinstance(expression, DatasetAggregateExpression):
                if expression.operand is not None:
                    values.append(expression.operand)
                if expression.filter is not None:
                    values.append(expression.filter)
            elif isinstance(expression, DatasetMetricExpression):
                continue
            else:
                values.append(expression)
        values.extend(stage.filters)
        values.extend(stage.group_by)
        return tuple(values)

    @staticmethod
    def _query_stage_metric_refs(stage: DatasetQueryStage) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.expression.ref
                for item in stage.projections
                if isinstance(item.expression, DatasetMetricExpression)
            )
        )

    @classmethod
    def _scalar_field_refs(cls, expression: DatasetScalarExpression) -> tuple[str, ...]:
        if isinstance(expression, DatasetFieldExpression):
            return (expression.ref,)
        if isinstance(expression, (DatasetOutputExpression, DatasetLiteralExpression)):
            return ()
        if isinstance(expression, DatasetUnaryExpression):
            return cls._scalar_field_refs(expression.operand)
        if isinstance(expression, DatasetBinaryExpression):
            return (*cls._scalar_field_refs(expression.left), *cls._scalar_field_refs(expression.right))
        assert isinstance(expression, DatasetFunctionExpression)
        return tuple(
            ref
            for argument in expression.arguments
            for ref in cls._scalar_field_refs(argument)
        )

    @classmethod
    def _scalar_output_refs(
        cls, expression: DatasetScalarExpression
    ) -> tuple[DatasetOutputExpression, ...]:
        if isinstance(expression, DatasetOutputExpression):
            return (expression,)
        if isinstance(expression, (DatasetFieldExpression, DatasetLiteralExpression)):
            return ()
        if isinstance(expression, DatasetUnaryExpression):
            return cls._scalar_output_refs(expression.operand)
        if isinstance(expression, DatasetBinaryExpression):
            return (
                *cls._scalar_output_refs(expression.left),
                *cls._scalar_output_refs(expression.right),
            )
        assert isinstance(expression, DatasetFunctionExpression)
        return tuple(
            output
            for argument in expression.arguments
            for output in cls._scalar_output_refs(argument)
        )

    @staticmethod
    def _json_object(value: str) -> dict[str, object]:
        text = value.strip()
        match = _FENCED_JSON.fullmatch(text)
        if match is not None:
            text = match.group(1)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end < start:
                raise ValueError("model did not return a JSON object")
            text = text[start : end + 1]
        document = json.loads(text)
        if not isinstance(document, dict):
            raise ValueError("dataset query program must be a JSON object")
        return document


__all__ = [
    "DatasetProgramPlanningResult",
    "DatasetQueryProgramPlanner",
    "ModelClient",
]
