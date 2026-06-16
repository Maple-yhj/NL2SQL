from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class Nl2SqlRequest(BaseModel):
    question: str = Field(min_length=1)
    tenant_id: str = Field(default="demo", min_length=1)
    execute: bool = False
    timeout_ms: int = Field(default=10_000, ge=1_000, le=60_000)
    max_limit: int = Field(default=1_000, ge=1, le=10_000)
    max_validation_attempts: int = Field(default=2, ge=1, le=5)

    @field_validator("question", "tenant_id")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class Nl2SqlResponse(BaseModel):
    ok: bool
    question: str
    tenant_id: str
    intent: dict[str, Any]
    sql: str
    rows: list[dict[str, Any]]
    answer: str
    error: str
    trace: list[dict[str, Any]]


class ErrorResponse(BaseModel):
    ok: bool = False
    error: str
    detail: str = ""
