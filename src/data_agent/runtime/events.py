"""Streaming event contracts emitted by a Data Agent runtime."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from data_agent.analysis_agent.models import AgentInputRequest, AnalysisPlan

from .errors import ErrorCode
from .models import (
    AgentArtifactSummary,
    AgentMode,
    AgentResponse,
    Digest,
    EvidenceSummary,
    NonBlankText,
    PublicContractModel,
    RuntimeVersionPins,
    SchemaFingerprint,
)


class AgentEventType(StrEnum):
    RUN_STARTED = "run_started"
    PROGRESS = "progress"
    CONTEXT_RESOLVED = "context_resolved"
    PLAN_UPDATED = "plan_updated"
    STEP_STARTED = "step_started"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    OBSERVATION_RECORDED = "observation_recorded"
    RUN_WAITING = "run_waiting"
    RUN_RESUMED = "run_resumed"
    ANSWER_SYNTHESIZING = "answer_synthesizing"
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


class ContextResolvedPayload(PublicContractModel):
    kind: Literal["context_resolved"] = "context_resolved"
    source_id: NonBlankText
    source_version: int = Field(ge=1)
    binding_id: NonBlankText
    binding_version: int = Field(ge=1)
    schema_fingerprint: SchemaFingerprint


class PlanUpdatedPayload(PublicContractModel):
    kind: Literal["plan_updated"] = "plan_updated"
    plan: AnalysisPlan


class StepStartedPayload(PublicContractModel):
    kind: Literal["step_started"] = "step_started"
    step_id: NonBlankText
    objective: NonBlankText


class ToolStartedPayload(PublicContractModel):
    kind: Literal["tool_started"] = "tool_started"
    call_id: NonBlankText
    action_id: NonBlankText
    tool_name: NonBlankText
    display_name: NonBlankText
    safe_arguments_digest: Digest


class ToolCompletedPayload(PublicContractModel):
    kind: Literal["tool_completed"] = "tool_completed"
    call_id: NonBlankText
    action_id: NonBlankText
    tool_name: NonBlankText
    status: Literal["succeeded", "failed"]
    artifacts: tuple[AgentArtifactSummary, ...] = ()
    evidence: tuple[EvidenceSummary, ...] = ()
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "ToolCompletedPayload":
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("failed tool event requires exactly one error code")
        return self


class ObservationRecordedPayload(PublicContractModel):
    kind: Literal["observation_recorded"] = "observation_recorded"
    observation_id: NonBlankText
    action_id: NonBlankText
    summary: NonBlankText
    artifact_ids: tuple[NonBlankText, ...] = ()
    evidence_ids: tuple[NonBlankText, ...] = ()

    @model_validator(mode="after")
    def validate_unique_refs(self) -> "ObservationRecordedPayload":
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("observation event artifact ids must be unique")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("observation event evidence ids must be unique")
        return self


class RunWaitingPayload(PublicContractModel):
    kind: Literal["run_waiting"] = "run_waiting"
    input_request: AgentInputRequest


class RunResumedPayload(PublicContractModel):
    kind: Literal["run_resumed"] = "run_resumed"
    interrupt_id: NonBlankText


class AnswerSynthesizingPayload(PublicContractModel):
    kind: Literal["answer_synthesizing"] = "answer_synthesizing"
    evidence_ids: tuple[NonBlankText, ...] = ()

    @model_validator(mode="after")
    def validate_unique_evidence(self) -> "AnswerSynthesizingPayload":
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("answer evidence ids must be unique")
        return self


class RunCompletedPayload(PublicContractModel):
    kind: Literal["run_completed"] = "run_completed"


class RunFailedPayload(PublicContractModel):
    kind: Literal["run_failed"] = "run_failed"
    error_code: ErrorCode


AgentEventPayload = Annotated[
    RunStartedPayload
    | RunProgressPayload
    | ContextResolvedPayload
    | PlanUpdatedPayload
    | StepStartedPayload
    | ToolStartedPayload
    | ToolCompletedPayload
    | ObservationRecordedPayload
    | RunWaitingPayload
    | RunResumedPayload
    | AnswerSynthesizingPayload
    | RunCompletedPayload
    | RunFailedPayload,
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
            AgentEventType.CONTEXT_RESOLVED: ContextResolvedPayload,
            AgentEventType.PLAN_UPDATED: PlanUpdatedPayload,
            AgentEventType.STEP_STARTED: StepStartedPayload,
            AgentEventType.TOOL_STARTED: ToolStartedPayload,
            AgentEventType.TOOL_COMPLETED: ToolCompletedPayload,
            AgentEventType.OBSERVATION_RECORDED: ObservationRecordedPayload,
            AgentEventType.RUN_WAITING: RunWaitingPayload,
            AgentEventType.RUN_RESUMED: RunResumedPayload,
            AgentEventType.ANSWER_SYNTHESIZING: AnswerSynthesizingPayload,
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

    @property
    def is_stream_closing(self) -> bool:
        return self.type in {
            AgentEventType.RUN_WAITING,
            AgentEventType.RUN_COMPLETED,
            AgentEventType.RUN_FAILED,
        }


__all__ = [
    "AgentEvent",
    "AgentEventPayload",
    "AgentEventType",
    "AnswerSynthesizingPayload",
    "ContextResolvedPayload",
    "ObservationRecordedPayload",
    "PlanUpdatedPayload",
    "RunCompletedPayload",
    "RunFailedPayload",
    "RunProgressPayload",
    "RunResumedPayload",
    "RunStartedPayload",
    "RunWaitingPayload",
    "StepStartedPayload",
    "ToolCompletedPayload",
    "ToolStartedPayload",
]
