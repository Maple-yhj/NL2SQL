"""Shared connector contract and stable connector error codes."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, runtime_checkable

from data_agent.dataset_query.contracts import PreparedQuery

from ..models import AccessGrant, CredentialLease
from ..schemas import CatalogSnapshot, ConnectorCapabilities, ExplainResult, TabularResult


class ConnectorErrorCode(StrEnum):
    GRANT_EXPIRED = "GRANT_EXPIRED"
    GRANT_MISMATCH = "GRANT_MISMATCH"
    CREDENTIAL_EXPIRED = "CREDENTIAL_EXPIRED"
    CREDENTIAL_MISMATCH = "CREDENTIAL_MISMATCH"
    RELATION_NOT_ALLOWED = "RELATION_NOT_ALLOWED"
    ROW_LIMIT_EXCEEDED = "ROW_LIMIT_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    CATALOG_INVALID = "CATALOG_INVALID"


class ConnectorError(RuntimeError):
    def __init__(self, code: ConnectorErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@runtime_checkable
class DataSourceConnector(Protocol):
    """Structural contract implemented by governed datasource connectors."""

    def capabilities(self) -> ConnectorCapabilities: ...

    async def execute_readonly(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
        lease: CredentialLease,
    ) -> TabularResult: ...

    async def preview(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
        lease: CredentialLease,
        *,
        preview_rows: int,
    ) -> TabularResult: ...

    async def explain(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
        lease: CredentialLease,
    ) -> ExplainResult: ...

    async def introspect_schema(
        self,
        grant: AccessGrant,
        lease: CredentialLease,
        *,
        relations: Sequence[str] = (),
    ) -> CatalogSnapshot: ...


__all__ = [
    "ConnectorError",
    "ConnectorErrorCode",
    "DataSourceConnector",
]
