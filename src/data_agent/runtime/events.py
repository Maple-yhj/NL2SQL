"""Streaming event contracts emitted by a Data Agent runtime."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .errors import ErrorCode
from .models import (
    AgentMode,
    AgentResponse,
    NonBlankText,
    PublicContractModel,
    RuntimeVersionPins,
)


class AgentEventType(StrEnum):
    RUN_STARTED = "run_started"
    PROGRESS = "progress"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class RunStartedPayload(PublicContractModel):
    kind: Literal["run_started"] = "run_started"
    mode: AgentMode
    enterprise_id: NonBlankText
    domain_id: NonBlankText


class RunProgressPayload(PublicContractModel):
    kind: Literal["progress"] = "progress"
    stage: Literal["versions_pinned"] = "versions_pinned"
    pins: RuntimeVersionPins


class RunCompletedPayload(PublicContractModel):
    kind: Literal["run_completed"] = "run_completed"


class RunFailedPayload(PublicContractModel):
    kind: Literal["run_failed"] = "run_failed"
    error_code: ErrorCode


AgentEventPayload = Annotated[
    RunStartedPayload | RunProgressPayload | RunCompletedPayload | RunFailedPayload,
    Field(discriminator="kind"),
]


class AgentEvent(PublicContractModel):
    type: AgentEventType
    run_id: NonBlankText
    sequence: int = Field(ge=0)
    data: AgentEventPayload
    response: AgentResponse | None = None

    @model_validator(mode="after")
    def validate_type_payload_and_terminal(self) -> "AgentEvent":
        expected_payload = {
            AgentEventType.RUN_STARTED: RunStartedPayload,
            AgentEventType.PROGRESS: RunProgressPayload,
            AgentEventType.RUN_COMPLETED: RunCompletedPayload,
            AgentEventType.RUN_FAILED: RunFailedPayload,
        }[self.type]
        if not isinstance(self.data, expected_payload):
            raise ValueError("event type and payload kind do not match")
        terminal = self.type in {
            AgentEventType.RUN_COMPLETED,
            AgentEventType.RUN_FAILED,
        }
        if terminal != (self.response is not None):
            raise ValueError("exactly terminal events carry an AgentResponse")
        if self.type == AgentEventType.RUN_COMPLETED and not self.response.ok:
            raise ValueError("completed event requires a successful response")
        if self.type == AgentEventType.RUN_FAILED and self.response.ok:
            raise ValueError("failed event requires an unsuccessful response")
        if (
            self.type == AgentEventType.RUN_FAILED
            and self.response is not None
            and self.response.error is not None
            and self.data.error_code != self.response.error.code
        ):
            raise ValueError("failed event payload and response error code do not match")
        return self


__all__ = [
    "AgentEvent",
    "AgentEventPayload",
    "AgentEventType",
    "RunCompletedPayload",
    "RunFailedPayload",
    "RunProgressPayload",
    "RunStartedPayload",
]
