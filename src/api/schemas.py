"""Strict HTTP schemas for auth and Runtime-backed product routes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from data_agent.runtime.models import (
    AgentRequest,
    AgentResponse,
    ConversationMessage,
    ConversationSummary,
)
from data_agent.runtime.events import AgentEvent
from data_agent.memory import ApprovalDecision, MemoryProposal, ProposalStatus
from data_agent.datasources import (
    DataSourceKind,
    DataSourceStatus,
    SemanticBindingRecord,
    SemanticFieldMapping,
    SemanticRelationship,
)
from data_agent.tools.schemas import CatalogSnapshot


def _strip_required_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


class StrictApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictApiModel):
    tenant_id: str = Field(default="demo", min_length=1)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)

    @field_validator("tenant_id", "username", "password")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return _strip_required_text(value)


class RefreshRequest(StrictApiModel):
    refresh_token: str = Field(min_length=1)

    @field_validator("refresh_token")
    @classmethod
    def strip_refresh_token(cls, value: str) -> str:
        return _strip_required_text(value)


class LogoutRequest(StrictApiModel):
    refresh_token: str = ""

    @field_validator("refresh_token")
    @classmethod
    def strip_optional_refresh_token(cls, value: str) -> str:
        return value.strip()


class AuthUserResponse(StrictApiModel):
    tenant_id: str
    user_id: str
    username: str
    roles: list[str]


class TokenResponse(StrictApiModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AuthUserResponse


class AccessTokenResponse(StrictApiModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class LogoutResponse(StrictApiModel):
    ok: bool = True


class Nl2SqlRequest(AgentRequest):
    """HTTP defaults target only user-provided datasets."""

    enterprise_id: str = Field(default="user-dataset", min_length=1)
    domain_id: str = Field(default="dataset", min_length=1)

    @field_validator("enterprise_id", "domain_id")
    @classmethod
    def strip_dataset_scope(cls, value: str) -> str:
        return _strip_required_text(value)


Nl2SqlResponse = AgentResponse


class ConversationCreateRequest(StrictApiModel):
    title: str = ""
    domain_id: str = Field(default="dataset", min_length=1)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("domain_id")
    @classmethod
    def strip_domain_id(cls, value: str) -> str:
        return _strip_required_text(value)


class ConversationUpdateRequest(StrictApiModel):
    title: str | None = None
    archived: bool | None = None

    @field_validator("title")
    @classmethod
    def strip_optional_title(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ConversationMessageRequest(AgentRequest):
    """Conversation turns default to the user-dataset runtime scope."""

    enterprise_id: str = Field(default="user-dataset", min_length=1)
    domain_id: str = Field(default="dataset", min_length=1)

    @field_validator("enterprise_id", "domain_id")
    @classmethod
    def strip_dataset_scope(cls, value: str) -> str:
        return _strip_required_text(value)


ConversationResponse = ConversationSummary


class ConversationListResponse(StrictApiModel):
    items: list[ConversationSummary]


class ConversationMessagesResponse(StrictApiModel):
    items: list[ConversationMessage]


ConversationNl2SqlResponse = AgentResponse


class DataSourceResponse(StrictApiModel):
    source_id: str
    name: str
    kind: DataSourceKind
    status: DataSourceStatus
    active_snapshot_version: int
    options: dict[str, str | int | float | bool]
    created_at: str
    updated_at: str


class DataSourceListResponse(StrictApiModel):
    items: list[DataSourceResponse]


class DataSourceDeleteResponse(StrictApiModel):
    source_id: str
    deleted: bool = True


class DataSourceCatalogResponse(StrictApiModel):
    source_id: str
    version: int
    fingerprint: str
    catalog: CatalogSnapshot


class PostgresDataSourceRequest(StrictApiModel):
    source_id: str | None = None
    name: str = Field(min_length=1)
    credential_ref: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1)
    ssl_mode: str = Field(default="require", min_length=1)

    @field_validator(
        "source_id",
        "name",
        "credential_ref",
        "host",
        "database",
        "ssl_mode",
    )
    @classmethod
    def strip_datasource_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class SemanticBindingCreateRequest(StrictApiModel):
    binding_id: str | None = None
    domain_id: str = Field(min_length=1)
    mappings: tuple[SemanticFieldMapping, ...] = Field(min_length=1)
    primary_relation: str | None = None
    relationships: tuple[SemanticRelationship, ...] = ()

    @field_validator("binding_id", "domain_id", "primary_relation")
    @classmethod
    def strip_binding_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class SemanticBindingListResponse(StrictApiModel):
    items: list[SemanticBindingRecord]


class ConversationDataSourceBindingResponse(StrictApiModel):
    binding: SemanticBindingRecord | None = None


class RunCancelResponse(StrictApiModel):
    run_id: str
    cancelled: bool


class RunEventListResponse(StrictApiModel):
    items: list[AgentEvent]


class MemoryProposalListResponse(StrictApiModel):
    items: list[MemoryProposal]


class MemoryProposalDecisionRequest(StrictApiModel):
    decision: ApprovalDecision
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def strip_decision_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class MemoryProposalDecisionResponse(StrictApiModel):
    proposal_id: str
    status: ProposalStatus


__all__ = [
    "AccessTokenResponse",
    "AuthUserResponse",
    "ConversationCreateRequest",
    "ConversationDataSourceBindingResponse",
    "ConversationListResponse",
    "ConversationMessage",
    "ConversationMessageRequest",
    "ConversationMessagesResponse",
    "ConversationNl2SqlResponse",
    "ConversationResponse",
    "ConversationUpdateRequest",
    "DataSourceCatalogResponse",
    "DataSourceDeleteResponse",
    "DataSourceListResponse",
    "DataSourceResponse",
    "LoginRequest",
    "LogoutRequest",
    "LogoutResponse",
    "MemoryProposalDecisionRequest",
    "MemoryProposalDecisionResponse",
    "MemoryProposalListResponse",
    "Nl2SqlRequest",
    "Nl2SqlResponse",
    "RefreshRequest",
    "RunCancelResponse",
    "RunEventListResponse",
    "PostgresDataSourceRequest",
    "SemanticBindingCreateRequest",
    "SemanticBindingListResponse",
    "TokenResponse",
]
