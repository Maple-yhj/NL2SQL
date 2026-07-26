"""Versioned control-plane models for user-selected datasources."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
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


class SemanticFieldMapping(DataSourceModel):
    logical_ref: NonBlankText
    physical_relation: NonBlankText
    physical_column: NonBlankText


class SemanticBindingRecord(DataSourceModel):
    binding_id: StableIdentifier
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    source_snapshot_version: int = Field(ge=1)
    domain_id: StableIdentifier
    version: int = Field(ge=1)
    status: SemanticBindingStatus = SemanticBindingStatus.DRAFT
    mappings: tuple[SemanticFieldMapping, ...] = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_binding(self) -> "SemanticBindingRecord":
        logical_refs = tuple(item.logical_ref for item in self.mappings)
        if len(logical_refs) != len(set(logical_refs)):
            raise ValueError("semantic binding logical references must be unique")
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


__all__ = [
    "ConversationDataSourcePin",
    "DataSourceDefinition",
    "DataSourceKind",
    "DataSourceSnapshot",
    "DataSourceStatus",
    "SemanticBindingRecord",
    "SemanticBindingStatus",
    "SemanticFieldMapping",
]
