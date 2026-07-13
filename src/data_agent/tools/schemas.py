"""Shared typed payload fragments used by connectors and Tool providers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import Field, StringConstraints

from .models import ToolModel


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CellValue = str | int | float | bool | Decimal | date | datetime | None


class QueryRow(ToolModel):
    values: tuple[CellValue, ...]


class TabularResult(ToolModel):
    columns: tuple[NonBlankText, ...]
    rows: tuple[QueryRow, ...]
    truncated: bool = False


class CatalogColumn(ToolModel):
    name: NonBlankText
    data_type: NonBlankText
    nullable: bool


class CatalogRelation(ToolModel):
    relation: NonBlankText
    columns: tuple[CatalogColumn, ...]
    estimated_rows: int | None = Field(default=None, ge=0)
    freshness_at: datetime | None = None


class CatalogSnapshot(ToolModel):
    schema_fingerprint: NonBlankText
    relations: tuple[CatalogRelation, ...]


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
