"""Shared typed payload fragments used by connectors and Tool providers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import unicodedata
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .models import ToolModel


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
StableCatalogId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$",
    ),
]
CellValue = str | int | float | bool | Decimal | date | datetime | None


def _normalized_identifier(value: str) -> str:
    """Normalize physical identifiers before deriving durable catalog IDs."""

    return unicodedata.normalize("NFKC", value).strip().casefold()


def stable_catalog_id(kind: str, *parts: str) -> str:
    """Create a deterministic, non-secret ID from normalized physical names."""

    normalized = "\0".join(_normalized_identifier(part) for part in parts)
    digest = hashlib.sha256(f"catalog-v1\0{kind}\0{normalized}".encode()).hexdigest()
    return f"{kind}:{digest[:32]}"


def catalog_schema_fingerprint(relations: tuple["CatalogRelation", ...]) -> str:
    """Fingerprint every catalog attribute that can affect relationship safety."""

    payload = [
        relation.model_dump(mode="json")
        for relation in sorted(relations, key=lambda item: item.relation_id)
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(f"catalog-schema-v2\0{encoded}".encode()).hexdigest()


class QueryRow(ToolModel):
    values: tuple[CellValue, ...]


class TabularResult(ToolModel):
    columns: tuple[NonBlankText, ...]
    rows: tuple[QueryRow, ...]
    truncated: bool = False


class CatalogColumn(ToolModel):
    # ``legacy`` is deliberately replaced by CatalogRelation when a column is
    # nested in a catalog.  Keeping a default makes pre-v2 persisted catalogs
    # readable without a migration.
    column_id: StableCatalogId = "legacy"
    name: NonBlankText
    data_type: NonBlankText
    nullable: bool
    ordinal: int = Field(default=0, ge=0)


class CatalogKey(ToolModel):
    key_id: StableCatalogId = "legacy"
    kind: Literal["primary", "unique"]
    column_ids: tuple[StableCatalogId, ...] = Field(min_length=1)


class CatalogForeignKey(ToolModel):
    foreign_key_id: StableCatalogId = "legacy"
    from_relation_id: StableCatalogId
    from_column_ids: tuple[StableCatalogId, ...] = Field(min_length=1)
    to_relation_id: StableCatalogId
    to_column_ids: tuple[StableCatalogId, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_arity(self) -> "CatalogForeignKey":
        if len(self.from_column_ids) != len(self.to_column_ids):
            raise ValueError("foreign key column counts must match")
        return self


class CatalogRelation(ToolModel):
    relation_id: StableCatalogId = "legacy"
    relation: NonBlankText
    columns: tuple[CatalogColumn, ...]
    keys: tuple[CatalogKey, ...] = ()
    foreign_keys: tuple[CatalogForeignKey, ...] = ()
    estimated_rows: int | None = Field(default=None, ge=0)
    freshness_at: datetime | None = None

    @model_validator(mode="after")
    def normalize_catalog_ids(self) -> "CatalogRelation":
        relation_id = (
            self.relation_id
            if self.relation_id != "legacy"
            else stable_catalog_id("relation", self.relation)
        )
        columns = tuple(
            column.model_copy(
                update={
                    "column_id": (
                        column.column_id
                        if column.column_id != "legacy"
                        else stable_catalog_id("column", self.relation, column.name)
                    ),
                    "ordinal": column.ordinal or index,
                }
            )
            for index, column in enumerate(self.columns, start=1)
        )
        column_ids = {column.column_id for column in columns}
        if len(column_ids) != len(columns):
            raise ValueError("catalog columns must have unique IDs")
        keys = tuple(
            key.model_copy(
                update={
                    "key_id": (
                        key.key_id
                        if key.key_id != "legacy"
                        else stable_catalog_id("key", self.relation, key.kind, *key.column_ids)
                    )
                }
            )
            for key in self.keys
        )
        if any(not set(key.column_ids).issubset(column_ids) for key in keys):
            raise ValueError("catalog key references an unknown column")
        foreign_keys = tuple(
            foreign_key.model_copy(
                update={
                    "foreign_key_id": (
                        foreign_key.foreign_key_id
                        if foreign_key.foreign_key_id != "legacy"
                        else stable_catalog_id(
                            "foreign-key",
                            self.relation,
                            *foreign_key.from_column_ids,
                            foreign_key.to_relation_id,
                            *foreign_key.to_column_ids,
                        )
                    )
                }
            )
            for foreign_key in self.foreign_keys
        )
        if any(
            foreign_key.from_relation_id != relation_id
            or not set(foreign_key.from_column_ids).issubset(column_ids)
            for foreign_key in foreign_keys
        ):
            raise ValueError("catalog foreign key has an invalid local reference")
        # Pydantic re-validates model_copy() updates; assigning through the
        # low-level API here avoids recursively re-entering this normalizer.
        object.__setattr__(self, "relation_id", relation_id)
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "keys", keys)
        object.__setattr__(self, "foreign_keys", foreign_keys)
        return self


class CatalogSnapshot(ToolModel):
    schema_fingerprint: NonBlankText
    relations: tuple[CatalogRelation, ...]

    @model_validator(mode="after")
    def validate_references(self) -> "CatalogSnapshot":
        relation_ids = {relation.relation_id for relation in self.relations}
        if len(relation_ids) != len(self.relations):
            raise ValueError("catalog relations must have unique IDs")
        all_column_ids = {
            column.column_id
            for relation in self.relations
            for column in relation.columns
        }
        for relation in self.relations:
            for foreign_key in relation.foreign_keys:
                if foreign_key.to_relation_id not in relation_ids or not set(
                    foreign_key.to_column_ids
                ).issubset(all_column_ids):
                    raise ValueError("catalog foreign key has an invalid target reference")
        return self


class ExplainResult(ToolModel):
    plan_text: NonBlankText
    estimated_cost: float | None = Field(default=None, ge=0)
    estimated_rows: int | None = Field(default=None, ge=0)


class ConnectorCapabilities(ToolModel):
    dialect: str = "postgres"
    read_only: bool = True
    supports_explain: bool = True
    supports_introspection: bool = True
    supports_cancellation: bool = True
