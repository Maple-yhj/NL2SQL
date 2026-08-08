"""Provider-neutral logical query models for user-selected datasets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SafeAlias = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]{0,62}$"),
]
Scalar = str | int | float | bool


class DatasetPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetPlanStatus(StrEnum):
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"


class DatasetFilterOperator(StrEnum):
    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    CONTAINS = "contains"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class DatasetFilter(DatasetPlanModel):
    ref: NonBlankText
    operator: DatasetFilterOperator
    value: Scalar | tuple[Scalar, ...] | None = None

    @model_validator(mode="after")
    def validate_value(self) -> "DatasetFilter":
        if self.operator in {
            DatasetFilterOperator.IS_NULL,
            DatasetFilterOperator.IS_NOT_NULL,
        }:
            if self.value is not None:
                raise ValueError("null filters cannot include a value")
        elif self.operator == DatasetFilterOperator.IN:
            if not isinstance(self.value, tuple) or not self.value:
                raise ValueError("in filters require a non-empty value list")
        elif self.value is None or isinstance(self.value, tuple):
            raise ValueError("scalar filters require one scalar value")
        return self


class DatasetAggregation(DatasetPlanModel):
    ref: NonBlankText
    operation: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    alias: SafeAlias


class DatasetOrdering(DatasetPlanModel):
    ref: NonBlankText
    direction: Literal["asc", "desc"] = "asc"


class DatasetQueryPlan(DatasetPlanModel):
    status: DatasetPlanStatus = DatasetPlanStatus.READY
    clarification_question: str | None = None
    analysis_type: Literal["detail", "aggregate"] | None = None
    select: tuple[NonBlankText, ...] = ()
    aggregations: tuple[DatasetAggregation, ...] = ()
    group_by: tuple[NonBlankText, ...] = ()
    filters: tuple[DatasetFilter, ...] = ()
    order_by: tuple[DatasetOrdering, ...] = ()
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_shape(self) -> "DatasetQueryPlan":
        if self.status != DatasetPlanStatus.READY:
            if not (self.clarification_question or "").strip():
                raise ValueError("non-ready plans require a clarification question")
            if (
                self.analysis_type is not None
                or self.select
                or self.aggregations
                or self.group_by
                or self.filters
                or self.order_by
            ):
                raise ValueError("non-ready plans cannot include executable query fields")
            return self
        if self.clarification_question is not None:
            raise ValueError("ready plans cannot include a clarification question")
        if self.analysis_type == "detail":
            if not self.select or self.aggregations or self.group_by:
                raise ValueError("detail plans require select fields only")
        elif self.analysis_type == "aggregate":
            if not self.aggregations:
                raise ValueError("aggregate plans require aggregations")
        else:
            raise ValueError("ready plans require an analysis type")
        aliases = tuple(item.alias for item in self.aggregations)
        if len(aliases) != len(set(aliases)):
            raise ValueError("aggregation aliases must be unique")
        for values in (self.select, self.group_by):
            if len(values) != len(set(values)):
                raise ValueError("logical refs must be unique")
        return self


class DatasetPlanPatch(DatasetPlanModel):
    status: DatasetPlanStatus = DatasetPlanStatus.READY
    clarification_question: str | None = None
    analysis_type: Literal["detail", "aggregate"] | None = None
    select: tuple[NonBlankText, ...] | None = None
    aggregations: tuple[DatasetAggregation, ...] | None = None
    group_by: tuple[NonBlankText, ...] | None = None
    filters: tuple[DatasetFilter, ...] | None = None
    add_filters: tuple[DatasetFilter, ...] = ()
    order_by: tuple[DatasetOrdering, ...] | None = None
    limit: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_patch(self) -> "DatasetPlanPatch":
        if self.status == DatasetPlanStatus.READY:
            if self.clarification_question is not None:
                raise ValueError("ready patches cannot include a clarification question")
            return self
        if not (self.clarification_question or "").strip():
            raise ValueError("non-ready patches require a clarification question")
        executable = (
            self.analysis_type,
            self.select,
            self.aggregations,
            self.group_by,
            self.filters,
            self.order_by,
            self.limit,
        )
        if any(item is not None for item in executable) or self.add_filters:
            raise ValueError("non-ready patches cannot include executable query fields")
        return self

    def apply(self, prior: "DatasetQueryPlan") -> "DatasetQueryPlan":
        if self.status != DatasetPlanStatus.READY:
            return DatasetQueryPlan(
                status=self.status,
                clarification_question=self.clarification_question,
            )
        if prior.status != DatasetPlanStatus.READY:
            raise ValueError("cannot patch a non-ready dataset query plan")
        filters = self.filters if self.filters is not None else prior.filters + self.add_filters
        return DatasetQueryPlan(
            analysis_type=self.analysis_type or prior.analysis_type,
            select=prior.select if self.select is None else self.select,
            aggregations=prior.aggregations if self.aggregations is None else self.aggregations,
            group_by=prior.group_by if self.group_by is None else self.group_by,
            filters=filters,
            order_by=prior.order_by if self.order_by is None else self.order_by,
            limit=prior.limit if self.limit is None else self.limit,
        )


class DatasetPlanUpdate(DatasetPlanModel):
    mode: Literal["patch", "replace"]
    patch: DatasetPlanPatch | None = None
    plan: DatasetQueryPlan | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "DatasetPlanUpdate":
        if self.mode == "patch":
            if self.patch is None or self.plan is not None:
                raise ValueError("patch updates require only patch")
        elif self.plan is None or self.patch is not None:
            raise ValueError("replace updates require only plan")
        return self


@dataclass(frozen=True, slots=True)
class DatasetConversationContext:
    prior_question: str
    prior_plan: DatasetQueryPlan


@dataclass(frozen=True, slots=True)
class DatasetPlanningResult:
    plan: DatasetQueryPlan
    contextualized_question: str


__all__ = [
    "DatasetAggregation",
    "DatasetConversationContext",
    "DatasetFilter",
    "DatasetFilterOperator",
    "DatasetOrdering",
    "DatasetPlanPatch",
    "DatasetPlanningResult",
    "DatasetPlanStatus",
    "DatasetPlanUpdate",
    "DatasetQueryPlan",
    "Scalar",
]
