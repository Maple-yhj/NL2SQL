"""Streaming event contracts emitted by a Data Agent runtime."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from .models import ContractModel, NonBlankText


class AgentEventType(StrEnum):
    RUN_STARTED = "run_started"
    PROGRESS = "progress"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class AgentEvent(ContractModel):
    type: AgentEventType
    run_id: NonBlankText
    sequence: int = Field(ge=0)
    data: dict[str, Any] = Field(default_factory=dict)
