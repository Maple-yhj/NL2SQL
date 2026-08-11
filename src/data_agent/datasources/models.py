"""Versioned control-plane models for user-selected datasources."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from data_agent.tools.schemas import CatalogSnapshot


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
StableIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$",
    ),
]
SourceOption = str | int | float | bool


class DataSourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    def model_copy(
        self,
        *,
        update: dict[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        fields = type(self).model_fields
        unknown = set(update) - set(fields)
        if unknown:
            raise ValueError(
                "model_copy update contains unknown fields: "
                + ", ".join(sorted(unknown))
            )
        values = {name: getattr(self, name) for name in fields}
        values.update(update)
        return type(self).model_validate(values)


class DataSourceKind(StrEnum):
    POSTGRES = "postgres"
    SQLITE = "sqlite"
    XLSX = "xlsx"
    CSV = "csv"


class DataSourceStatus(StrEnum):
    REGISTERED = "registered"
    READY = "ready"
    ERROR = "error"
    DISABLED = "disabled"


class SemanticBindingStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class SemanticJoinType(StrEnum):
    INNER = "inner"
    LEFT = "left"


class SemanticFieldMetadata(DataSourceModel):
    display_name: NonBlankText | None = None
    description: NonBlankText | None = None
    semantic_role: Literal[
        "identifier",
        "dimension",
        "measure",
        "time",
        "status",
        "attribute",
    ] | None = None
    entity: NonBlankText | None = None
    grain: NonBlankText | None = None
    unit: NonBlankText | None = None
    lifecycle_stage: NonBlankText | None = None
    synonyms: tuple[NonBlankText, ...] = ()

    @field_validator("synonyms")
    @classmethod
    def validate_unique_synonyms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(item.casefold() for item in values)):
            raise ValueError("semantic field synonyms must be unique")
        return values


class SemanticMetricDefinition(DataSourceModel):
    metric_ref: NonBlankText
    display_name: NonBlankText
    description: NonBlankText
    operation: Literal[
        "count",
        "count_distinct",
        "sum",
        "avg",
        "min",
        "max",
        "median",
    ]
    field_ref: NonBlankText | None = None
    unit: NonBlankText | None = None
    grain: NonBlankText | None = None
    synonyms: tuple[NonBlankText, ...] = ()

    @field_validator("synonyms")
    @classmethod
    def validate_unique_metric_synonyms(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(values) != len(set(item.casefold() for item in values)):
            raise ValueError("semantic metric synonyms must be unique")
        return values

    @model_validator(mode="after")
    def validate_metric_shape(self) -> "SemanticMetricDefinition":
        if self.operation != "count" and self.field_ref is None:
            raise ValueError("non-count metrics require a logical field_ref")
        return self


class DataSourceDefinition(DataSourceModel):
    source_id: StableIdentifier
    tenant_id: StableIdentifier
    name: NonBlankText
    kind: DataSourceKind
    credential_ref: NonBlankText | None = None
    location_ref: NonBlankText | None = None
    options: dict[NonBlankText, SourceOption] = Field(default_factory=dict)
    revision: int = Field(default=1, ge=1)
    active_snapshot_version: int = Field(default=0, ge=0)
    status: DataSourceStatus = DataSourceStatus.REGISTERED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_source_authority(self) -> "DataSourceDefinition":
        if self.kind == DataSourceKind.POSTGRES:
            if self.credential_ref is None:
                raise ValueError("PostgreSQL datasource requires credential_ref")
        elif self.location_ref is None:
            raise ValueError("file datasource requires an internal location_ref")
        forbidden = {
            key
            for key in self.options
            if any(
                marker in key.lower()
                for marker in ("password", "passwd", "secret", "token", "api_key")
            )
        }
        if forbidden:
            raise ValueError(
                "datasource options cannot contain secrets: "
                + ", ".join(sorted(forbidden))
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class DataSourceSnapshot(DataSourceModel):
    snapshot_id: StableIdentifier
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    version: int = Field(ge=1)
    fingerprint: NonBlankText
    catalog: CatalogSnapshot
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_snapshot(self) -> "DataSourceSnapshot":
        if self.catalog.schema_fingerprint != self.fingerprint:
            raise ValueError("snapshot fingerprint must match the catalog")
        relations = tuple(item.relation for item in self.catalog.relations)
        if len(relations) != len(set(relations)):
            raise ValueError("snapshot catalog relations must be unique")
        for relation in self.catalog.relations:
            columns = tuple(item.name for item in relation.columns)
            if len(columns) != len(set(columns)):
                raise ValueError(
                    f"snapshot catalog columns must be unique for {relation.relation}"
                )
        return self


class SemanticFieldMapping(SemanticFieldMetadata):
    logical_ref: NonBlankText
    physical_relation: NonBlankText
    physical_column: NonBlankText


class SemanticRelationship(DataSourceModel):
    relationship_id: StableIdentifier
    left_relation: NonBlankText
    left_column: NonBlankText
    right_relation: NonBlankText
    right_column: NonBlankText
    join_type: SemanticJoinType = SemanticJoinType.INNER

    @model_validator(mode="after")
    def validate_relationship(self) -> "SemanticRelationship":
        if self.left_relation == self.right_relation:
            raise ValueError("dataset relationships must connect two different relations")
        return self


class SemanticBindingRecord(DataSourceModel):
    schema_version: Literal[1] = 1
    binding_id: StableIdentifier
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    source_snapshot_version: int = Field(ge=1)
    domain_id: StableIdentifier
    version: int = Field(ge=1)
    status: SemanticBindingStatus = SemanticBindingStatus.DRAFT
    mappings: tuple[SemanticFieldMapping, ...] = Field(min_length=1)
    metrics: tuple[SemanticMetricDefinition, ...] = ()
    primary_relation: NonBlankText | None = None
    relationships: tuple[SemanticRelationship, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_binding(self) -> "SemanticBindingRecord":
        logical_refs = tuple(item.logical_ref for item in self.mappings)
        if len(logical_refs) != len(set(logical_refs)):
            raise ValueError("semantic binding logical references must be unique")
        metric_refs = tuple(item.metric_ref for item in self.metrics)
        if len(metric_refs) != len(set(metric_refs)):
            raise ValueError("semantic metric references must be unique")
        if set(metric_refs).intersection(logical_refs):
            raise ValueError("semantic metrics and fields must use different refs")
        unknown_metric_fields = {
            item.field_ref
            for item in self.metrics
            if item.field_ref is not None and item.field_ref not in logical_refs
        }
        if unknown_metric_fields:
            raise ValueError("semantic metrics reference unknown logical fields")
        relationship_ids = tuple(
            item.relationship_id for item in self.relationships
        )
        if len(relationship_ids) != len(set(relationship_ids)):
            raise ValueError("semantic binding relationship identifiers must be unique")
        mapped_relations = tuple(
            dict.fromkeys(item.physical_relation for item in self.mappings)
        )
        mapped_relation_set = set(mapped_relations)
        if self.primary_relation is not None:
            if self.primary_relation not in mapped_relation_set:
                raise ValueError("primary relation must have at least one field mapping")
        elif len(mapped_relations) > 1:
            raise ValueError("multi-relation bindings require a primary relation")
        connected = {
            self.primary_relation
            if self.primary_relation is not None
            else mapped_relations[0]
        }
        relationship_pairs: set[tuple[str, str, str, str]] = set()
        for relationship in self.relationships:
            if (
                relationship.left_relation not in mapped_relation_set
                or relationship.right_relation not in mapped_relation_set
            ):
                raise ValueError(
                    "dataset relationships must reference mapped relations"
                )
            pair = (
                relationship.left_relation,
                relationship.left_column,
                relationship.right_relation,
                relationship.right_column,
            )
            if pair in relationship_pairs:
                raise ValueError("semantic binding relationships must be unique")
            relationship_pairs.add(pair)
            if relationship.left_relation not in connected:
                raise ValueError(
                    "dataset relationships must extend from the connected dataset"
                )
            if relationship.right_relation in connected:
                raise ValueError(
                    "dataset relationships cannot introduce cycles or duplicate tables"
                )
            connected.add(relationship.right_relation)
        if connected != mapped_relation_set:
            raise ValueError(
                "every mapped relation must be connected to the primary relation"
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class SemanticGraphFieldMapping(SemanticFieldMetadata):
    logical_ref: NonBlankText
    node_id: StableIdentifier
    column_id: NonBlankText


class SemanticGraphBindingRecord(DataSourceModel):
    """Immutable v2 binding; editable relationship graphs never become active."""

    schema_version: Literal[2] = 2
    binding_id: StableIdentifier
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    source_snapshot_version: int = Field(ge=1)
    schema_fingerprint: NonBlankText
    domain_id: StableIdentifier
    version: int = Field(ge=1)
    status: SemanticBindingStatus = SemanticBindingStatus.DRAFT
    graph: "ActivatedRelationshipGraph"
    mappings: tuple[SemanticGraphFieldMapping, ...] = Field(min_length=1)
    metrics: tuple[SemanticMetricDefinition, ...] = ()
    validation_report_digest: NonBlankText
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_semantic_definitions(self) -> "SemanticGraphBindingRecord":
        logical_refs = tuple(item.logical_ref for item in self.mappings)
        if len(logical_refs) != len(set(logical_refs)):
            raise ValueError("semantic graph logical references must be unique")
        metric_refs = tuple(item.metric_ref for item in self.metrics)
        if len(metric_refs) != len(set(metric_refs)):
            raise ValueError("semantic metric references must be unique")
        if set(metric_refs).intersection(logical_refs):
            raise ValueError("semantic metrics and fields must use different refs")
        if any(
            item.field_ref is not None and item.field_ref not in logical_refs
            for item in self.metrics
        ):
            raise ValueError("semantic metrics reference unknown logical fields")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class ConversationDataSourcePin(DataSourceModel):
    tenant_id: StableIdentifier
    user_id: NonBlankText
    conversation_id: NonBlankText
    domain_id: StableIdentifier
    source_id: StableIdentifier
    source_version: int = Field(ge=1)
    binding_id: StableIdentifier
    binding_version: int = Field(ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Import late so the relationship package remains independent of the datasource
# control plane and historical v1 bindings keep loading without a migration.
from data_agent.relationships.models import ActivatedRelationshipGraph  # noqa: E402

SemanticGraphBindingRecord.model_rebuild()


__all__ = [
    "ConversationDataSourcePin",
    "DataSourceDefinition",
    "DataSourceKind",
    "DataSourceSnapshot",
    "DataSourceStatus",
    "SemanticBindingRecord",
    "SemanticGraphBindingRecord",
    "SemanticGraphFieldMapping",
    "SemanticBindingStatus",
    "SemanticFieldMapping",
    "SemanticFieldMetadata",
    "SemanticMetricDefinition",
    "SemanticJoinType",
    "SemanticRelationship",
]
