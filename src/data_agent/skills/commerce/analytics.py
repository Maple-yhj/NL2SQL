"""Trusted built-in implementation of ``commerce.analytics@1.0.0``."""

from __future__ import annotations

from data_agent.runtime.packs import DomainEvalCase, DomainPack

from ..models import (
    AnalysisType,
    CrossTabSpec,
    DerivedCalculation,
    LogicalFilter,
    LogicalOrdering,
    LogicalQueryPlan,
    PlanContext,
    RankingSpec,
    ResultShape,
    SeriesAxis,
    SkillInput,
    SkillManifest,
    TimeRange,
    WindowSpec,
)
from ..validation import (
    CommercePlanValidator,
    PlanValidationResult,
    expected_result_shape,
    relationship_ids_for_entities,
)


ALLOWED_TOOL_CAPABILITIES = (
    "semantic.search",
    "data.inspect",
    "query.compile",
    "query.execute",
    "result.profile",
    "answer.render",
)


COMMERCE_ANALYTICS_MANIFEST = SkillManifest(
    skill_id="commerce.analytics",
    version="1.0.0",
    domain="commerce",
    intent_signatures=(
        "commerce metric analysis",
        "commerce trend and comparison",
        "commerce ranking and detail",
        "commerce distribution and cross tab",
    ),
    required_semantic_ids=(
        "commerce.Order",
        "commerce.OrderItem",
        "commerce.Customer",
        "commerce.Seller",
        "commerce.Product",
        "commerce.Payment",
        "commerce.Review",
        "commerce.GeoLocation",
        "commerce.CategoryTranslation",
        "commerce.gmv",
        "commerce.order_count",
        "commerce.average_item_price",
        "commerce.average_review_score",
    ),
    required_tool_capabilities=ALLOWED_TOOL_CAPABILITIES,
    allowed_tools=ALLOWED_TOOL_CAPABILITIES,
    graph_fragment=(
        "commerce.semantic_resolution",
        "commerce.logical_planning",
        "commerce.plan_validation",
    ),
    logical_plan_schema="dataagent.io/skills/logical-query-plan/v1",
    validators=(
        "commerce.canonical_refs",
        "commerce.entity_reachability",
        "commerce.metric_grain",
        "commerce.fanout",
        "commerce.temporal",
        "commerce.order_limit",
        "commerce.result_shape",
    ),
    output_schema="dataagent.io/skills/plan-validation-result/v1",
    memory_write_policy="proposal_only",
    eval_suite_ref="commerce.evals@1.0.0",
)


def _window_specs(
    case: DomainEvalCase,
) -> tuple[WindowSpec, ...]:
    windows: list[WindowSpec] = []
    for calculation in case.calculations:
        if calculation.operation not in {"growth", "lag"}:
            continue
        if case.time is not None:
            ordering_ref = case.time.field
        elif case.expected_dimensions:
            ordering_ref = case.expected_dimensions[0]
        else:
            raise ValueError("window eval requires a canonical ordering field")
        windows.append(
            WindowSpec(
                id=f"{calculation.id}_window",
                calculation=calculation.id,
                axis_ref=ordering_ref,
                partition_by=calculation.partition_by,
                ordering=(
                    LogicalOrdering(ref=ordering_ref, direction="asc"),
                ),
                output_grain=case.expected_grain,
            )
        )
    return tuple(windows)


def _series_axis(
    case: DomainEvalCase,
    domain_pack: DomainPack,
) -> SeriesAxis | None:
    if case.time is not None and case.time.grain is not None:
        return SeriesAxis(
            kind="time",
            field=case.time.field,
            time_grain=case.time.grain,
        )
    if case.analysis_type != "trend":
        return None
    for field_ref in case.expected_dimensions:
        entity_id, field_name = field_ref.rsplit(".", 1)
        entity = domain_pack.spec.entities.get(entity_id)
        if entity is not None and entity.fields[field_name].type in {"integer", "decimal"}:
            return SeriesAxis(kind="numeric", field=field_ref)
    raise ValueError("non-temporal trend eval requires a numeric series axis")


def _ranking_spec(
    case: DomainEvalCase,
    analysis_type: AnalysisType,
    ordering: tuple[LogicalOrdering, ...],
    calculations: tuple[DerivedCalculation, ...],
) -> RankingSpec | None:
    if not ordering or case.expected_fields:
        return None
    outputs = set(case.expected_metrics) | {item.id for item in calculations}
    measure = ordering[0].ref
    if measure not in outputs:
        return None
    ranking_analysis = analysis_type == AnalysisType.RANKING
    contextual_ranking = analysis_type in {
        AnalysisType.TENANT_SCOPED,
        AnalysisType.FOLLOW_UP,
    }
    limited_aggregate = (
        case.limit is not None
        and analysis_type in {AnalysisType.TREND, AnalysisType.CROSS_TAB}
    )
    if not (ranking_analysis or contextual_ranking or limited_aggregate):
        return None
    return RankingSpec(
        mode="top_n" if case.limit is not None else "full",
        measure=measure,
    )


def _cross_tab_spec(
    case: DomainEvalCase,
    analysis_type: AnalysisType,
    calculations: tuple[DerivedCalculation, ...],
) -> CrossTabSpec | None:
    if analysis_type != AnalysisType.CROSS_TAB:
        return None
    values = (
        *case.expected_metrics,
        *(item.id for item in calculations),
    )
    if len(case.expected_dimensions) != 2:
        raise ValueError("cross-tab eval requires exactly two dimension axes")
    return CrossTabSpec(
        row_axis=case.expected_dimensions[0],
        column_axis=case.expected_dimensions[1],
        values=values,
    )


def _analysis_type(case: DomainEvalCase) -> AnalysisType:
    if case.analysis_type == "cross_tab" and len(case.expected_dimensions) < 2:
        return AnalysisType.COMPARISON
    return AnalysisType(case.analysis_type)


def logical_plan_from_eval_case(
    case: DomainEvalCase,
    domain_pack: DomainPack,
) -> LogicalQueryPlan:
    """Losslessly adapt one governed eval oracle into a typed plan fixture."""

    if domain_pack.metadata.name != "commerce":
        raise ValueError("commerce eval fixtures require the commerce DomainPack")
    if case not in domain_pack.spec.evals:
        raise ValueError("eval case must belong to the supplied DomainPack snapshot")

    analysis_type = _analysis_type(case)
    filters = tuple(
        LogicalFilter(
            ref=item.ref,
            operator=item.operator,
            value=item.value,
        )
        for item in case.filters
    )
    having = tuple(
        LogicalFilter(
            ref=item.ref,
            operator=item.operator,
            value=item.value,
        )
        for item in case.having
    )
    calculation_items: list[DerivedCalculation] = []
    for item in case.calculations:
        if item.operation == "count_distinct" and len(item.inputs) > 1:
            composite_id = f"{item.id}_key"
            calculation_items.append(
                DerivedCalculation(
                    id=composite_id,
                    operation="composite_key",
                    inputs=item.inputs,
                )
            )
            calculation_items.append(
                DerivedCalculation(
                    id=item.id,
                    operation=item.operation,
                    inputs=(composite_id,),
                    partition_by=item.partition_by,
                )
            )
            continue
        calculation_items.append(
            DerivedCalculation(
                id=item.id,
                operation=item.operation,
                inputs=item.inputs,
                partition_by=item.partition_by,
            )
        )
    calculations = tuple(calculation_items)
    time_range = (
        TimeRange(
            field=case.time.field,
            start=case.time.start,
            end=case.time.end,
        )
        if case.time is not None
        else None
    )
    relationships = relationship_ids_for_entities(
        case.expected_entities,
        domain_pack,
    )
    ordering = tuple(
        LogicalOrdering(ref=item.ref, direction=item.direction)
        for item in case.ordering
    )
    context = PlanContext(
        mode=case.context.mode,
        tenant_scope=case.context.tenant_scope,
        prior_question=case.context.prior_question,
        preserve=case.context.preserve,
    )
    series_axis = _series_axis(case, domain_pack)
    ranking = _ranking_spec(case, analysis_type, ordering, calculations)
    cross_tab = _cross_tab_spec(case, analysis_type, calculations)

    provisional = LogicalQueryPlan(
        analysis_type=analysis_type,
        metrics=case.expected_metrics,
        entities=case.expected_entities,
        relationships=relationships,
        dimensions=case.expected_dimensions,
        fields=case.expected_fields,
        filters=filters,
        time_range=time_range,
        time_grain=case.time.grain if case.time is not None else None,
        series_axis=series_axis,
        ordering=ordering,
        limit=case.limit,
        ranking=ranking,
        cross_tab=cross_tab,
        expected_grain=case.expected_grain,
        assumptions=(),
        requested_evidence=("semantic_resolution", "query_result"),
        derived_calculations=calculations,
        having=having,
        window_specs=_window_specs(case),
        grain_alignment=(),
        result_shape=ResultShape.SCALAR,
        context=context,
    )
    shape = expected_result_shape(provisional)
    limit = case.limit
    assumptions: tuple[str, ...] = ()
    if shape == ResultShape.DETAIL and limit is None:
        limit = 1000
        assumptions = ("bounded_detail_default",)
    if limit is not None and not ordering:
        ordering = tuple(
            LogicalOrdering(ref=ref, direction="asc")
            for ref in case.expected_grain
        )

    provisional = provisional.model_copy(
        update={
            "ordering": ordering,
            "limit": limit,
            "assumptions": assumptions,
            "result_shape": shape,
        }
    )
    validator = CommercePlanValidator()
    plan = provisional.model_copy(
        update={
            "grain_alignment": validator.suggest_grain_alignment(
                provisional,
                domain_pack,
            )
        }
    )
    validator.require_valid(plan, domain_pack)
    return plan


class CommerceAnalyticsSkill:
    """Versioned logical planning contract; no tool or graph execution lives here."""

    manifest = COMMERCE_ANALYTICS_MANIFEST

    def __init__(self) -> None:
        self._validator = CommercePlanValidator()

    @property
    def validator(self) -> CommercePlanValidator:
        return self._validator

    def validate(
        self,
        skill_input: SkillInput,
        plan: LogicalQueryPlan,
    ) -> PlanValidationResult:
        return self._validator.validate(
            plan,
            skill_input.commerce_semantic_snapshot,
        )

    def validate_plan(
        self,
        plan: LogicalQueryPlan,
        domain_pack: DomainPack,
    ) -> PlanValidationResult:
        return self._validator.validate(plan, domain_pack)

    def plan_from_eval_case(
        self,
        case: DomainEvalCase,
        domain_pack: DomainPack,
    ) -> LogicalQueryPlan:
        return logical_plan_from_eval_case(case, domain_pack)
