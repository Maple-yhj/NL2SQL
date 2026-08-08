"""Public event runtime over the checkpointed native analysis graph."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import uuid4

from langgraph.types import Command
from pydantic import Field

from data_agent.public_contracts import (
    AgentError,
    ErrorCode,
    NonBlankText,
    PublicContractModel,
)
from data_agent.runtime.events import (
    AgentEvent,
    AgentEventType,
    RunFailedPayload,
    RunStartedPayload,
)
from data_agent.runtime.models import AgentRequest, AgentResponse, PrincipalContext

from .graph import CompiledAnalysisGraph
from .models import AgentStatus, DatasetAuthority
from .nodes import AnalysisGraphContext


class AgentResumeRequest(PublicContractModel):
    interrupt_id: NonBlankText
    decision: Literal["respond"] = "respond"
    message: NonBlankText = Field(max_length=4000)
    edited_action: None = None


@dataclass(frozen=True, slots=True)
class ResolvedAnalysisRun:
    authority: DatasetAuthority
    graph_context: AnalysisGraphContext


class AnalysisRunResolver(Protocol):
    async def resolve(
        self,
        *,
        request: AgentRequest,
        principal: PrincipalContext,
        run_id: str,
    ) -> ResolvedAnalysisRun: ...


class AnalysisRuntimeError(RuntimeError):
    def __init__(self, error: AgentError) -> None:
        self.error = error
        super().__init__(error.message)


def _run_id() -> str:
    return "analysis-run-" + uuid4().hex


class DataAnalysisAgentRuntime:
    def __init__(
        self,
        *,
        graph: CompiledAnalysisGraph,
        resolver: AnalysisRunResolver,
        run_id_factory: Callable[[], str] = _run_id,
    ) -> None:
        self._graph = graph
        self._resolver = resolver
        self._run_id_factory = run_id_factory
        self._next_sequences: dict[str, int] = {}
        self._resume_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    async def run(
        self,
        request: AgentRequest,
        principal: PrincipalContext,
        *,
        run_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self._require_open()
        request = AgentRequest.model_validate(request)
        principal = PrincipalContext.model_validate(principal)
        resolved_run_id = run_id or self._run_id_factory()
        try:
            resolved = await self._resolver.resolve(
                request=request,
                principal=principal,
                run_id=resolved_run_id,
            )
            self._validate_resolved(request, principal, resolved)
        except AnalysisRuntimeError as exc:
            yield self._started_event(
                run_id=resolved_run_id,
                request=request,
            )
            yield self._failure_event(
                run_id=resolved_run_id,
                sequence=1,
                request=request,
                principal=principal,
                code=exc.error.code,
                message=exc.error.message,
            )
            return
        except Exception:
            yield self._started_event(
                run_id=resolved_run_id,
                request=request,
            )
            yield self._failure_event(
                run_id=resolved_run_id,
                sequence=1,
                request=request,
                principal=principal,
                code=ErrorCode.INTERNAL_ERROR,
                message="analysis context could not be resolved safely",
            )
            return
        config = self._config(
            run_id=resolved_run_id,
            request=request,
            principal=principal,
            authority=resolved.authority,
        )
        value = {
            "run_id": resolved_run_id,
            "conversation_id": request.conversation_id,
            "request": request,
            "authority": resolved.authority,
        }
        async for event in self._stream(
            value=value,
            run_id=resolved_run_id,
            request=request,
            principal=principal,
            context=resolved.graph_context,
            config=config,
            start_sequence=0,
        ):
            yield event

    async def resume(
        self,
        *,
        run_id: str,
        response: AgentResumeRequest,
        principal: PrincipalContext,
        start_sequence: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self._require_open()
        response = AgentResumeRequest.model_validate(response)
        principal = PrincipalContext.model_validate(principal)
        lock = self._resume_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            state, request, authority, config = await self._load_resumable_state(
                run_id=run_id,
                principal=principal,
                interrupt_id=response.interrupt_id,
            )
            try:
                resolved = await self._resolver.resolve(
                    request=request,
                    principal=principal,
                    run_id=run_id,
                )
            except AnalysisRuntimeError:
                raise
            except Exception as exc:
                raise AnalysisRuntimeError(
                    AgentError(
                        code=ErrorCode.INTERNAL_ERROR,
                        message="analysis context could not be resolved safely",
                    )
                ) from exc
            self._validate_resolved(request, principal, resolved)
            if resolved.authority != authority:
                raise AnalysisRuntimeError(
                    AgentError(
                        code=ErrorCode.BINDING_STALE,
                        message="dataset authority changed after the run was paused",
                    )
                )
            sequence = (
                self._next_sequences.get(run_id, 0)
                if start_sequence is None
                else start_sequence
            )
            command = Command(
                resume={
                    "interrupt_id": response.interrupt_id,
                    "message": response.message,
                }
            )
            async for event in self._stream(
                value=command,
                run_id=run_id,
                request=request,
                principal=principal,
                context=resolved.graph_context,
                config=config,
                start_sequence=sequence,
            ):
                yield event

    async def cancel_waiting(
        self,
        *,
        run_id: str,
        principal: PrincipalContext,
        start_sequence: int = 0,
    ) -> AgentEvent:
        self._require_open()
        principal = PrincipalContext.model_validate(principal)
        state, request, _, config = await self._load_resumable_state(
            run_id=run_id,
            principal=principal,
            interrupt_id=None,
        )
        if AgentStatus(state["status"]) != AgentStatus.WAITING_INPUT:
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.AGENT_RESUME_CONFLICT,
                    message="only a waiting analysis run can be cancelled",
                )
            )
        await self._graph.compiled_graph.aupdate_state(
            config,
            {
                "status": AgentStatus.CANCELLED,
                "error": AgentError(
                    code=ErrorCode.CANCELLED,
                    message="analysis run was cancelled while waiting for input",
                ),
                "waiting_request": None,
            },
        )
        return self._failure_event(
            run_id=run_id,
            sequence=start_sequence,
            request=request,
            principal=principal,
            code=ErrorCode.CANCELLED,
            message="analysis run was cancelled while waiting for input",
        )

    async def state(
        self,
        run_id: str,
        *,
        principal: PrincipalContext,
    ) -> dict[str, object]:
        self._require_open()
        state, _, _, _ = await self._load_checkpoint_state(
            run_id=run_id,
            principal=PrincipalContext.model_validate(principal),
        )
        return state

    async def close(self) -> None:
        self._closed = True
        self._resume_locks.clear()
        self._next_sequences.clear()

    async def _stream(
        self,
        *,
        value: object,
        run_id: str,
        request: AgentRequest,
        principal: PrincipalContext,
        context: AnalysisGraphContext,
        config: dict[str, object],
        start_sequence: int,
    ) -> AsyncIterator[AgentEvent]:
        next_sequence = start_sequence
        closed = False
        try:
            async for event in self._graph.astream_events(
                value,
                run_id=run_id,
                context=context,
                config=config,
                start_sequence=start_sequence,
            ):
                if closed:
                    raise RuntimeError(
                        "analysis graph emitted an event after the stream closed"
                    )
                if event.sequence != next_sequence:
                    raise RuntimeError("analysis graph event sequence is not contiguous")
                next_sequence += 1
                yield event
                if event.is_stream_closing:
                    closed = True
        except asyncio.CancelledError:
            try:
                await self._graph.compiled_graph.aupdate_state(
                    config,
                    {
                        "status": AgentStatus.CANCELLED,
                        "error": AgentError(
                            code=ErrorCode.CANCELLED,
                            message="analysis run was cancelled",
                        ),
                        "waiting_request": None,
                    },
                )
            except Exception:
                pass
            event = self._failure_event(
                run_id=run_id,
                sequence=next_sequence,
                request=request,
                principal=principal,
                code=ErrorCode.CANCELLED,
                message="analysis run was cancelled",
            )
            next_sequence += 1
            closed = True
            yield event
        except Exception:
            if not closed:
                event = self._failure_event(
                    run_id=run_id,
                    sequence=next_sequence,
                    request=request,
                    principal=principal,
                    code=ErrorCode.INTERNAL_ERROR,
                    message="analysis runtime failed safely",
                )
                next_sequence += 1
                closed = True
                yield event
        if not closed:
            event = self._failure_event(
                run_id=run_id,
                sequence=next_sequence,
                request=request,
                principal=principal,
                code=ErrorCode.INTERNAL_ERROR,
                message="analysis runtime ended without a terminal or waiting event",
            )
            next_sequence += 1
            yield event
        self._next_sequences[run_id] = next_sequence

    async def _load_resumable_state(
        self,
        *,
        run_id: str,
        principal: PrincipalContext,
        interrupt_id: str | None,
    ) -> tuple[
        dict[str, object],
        AgentRequest,
        DatasetAuthority,
        dict[str, object],
    ]:
        state, request, authority, config = await self._load_checkpoint_state(
            run_id=run_id,
            principal=principal,
        )
        status = AgentStatus(state["status"])
        if status != AgentStatus.WAITING_INPUT:
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.AGENT_RESUME_CONFLICT,
                    message="analysis run is not waiting for input",
                )
            )
        from .models import AgentInputRequest

        try:
            waiting = (
                AgentInputRequest.model_validate(state["waiting_request"])
                if state.get("waiting_request") is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.AGENT_CHECKPOINT_UNAVAILABLE,
                    message="analysis checkpoint interrupt payload is invalid",
                )
            ) from exc
        if interrupt_id is not None and (
            waiting is None
            or waiting.interrupt_id != interrupt_id
        ):
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.AGENT_INTERRUPT_STALE,
                    message="interrupt response does not match the latest checkpoint",
                )
            )
        return state, request, authority, config

    async def _load_checkpoint_state(
        self,
        *,
        run_id: str,
        principal: PrincipalContext,
    ) -> tuple[
        dict[str, object],
        AgentRequest,
        DatasetAuthority,
        dict[str, object],
    ]:
        config: dict[str, object] = {
            "configurable": {"thread_id": run_id}
        }
        try:
            snapshot = await self._graph.compiled_graph.aget_state(config)
        except Exception as exc:
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.AGENT_CHECKPOINT_UNAVAILABLE,
                    message="analysis checkpoint could not be loaded",
                    retryable=True,
                )
            ) from exc
        if not snapshot.values:
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.AGENT_CHECKPOINT_UNAVAILABLE,
                    message="analysis checkpoint was not found",
                )
            )
        state = dict(snapshot.values)
        try:
            request = AgentRequest.model_validate(state["request"])
            authority = DatasetAuthority.model_validate(state["authority"])
            AgentStatus(state["status"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.AGENT_CHECKPOINT_UNAVAILABLE,
                    message="analysis checkpoint is invalid",
                )
            ) from exc
        if (
            authority.tenant_id != principal.tenant_id
            or authority.user_id != principal.user_id
        ):
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.ACCESS_DENIED,
                    message="analysis run does not belong to the current principal",
                )
            )
        metadata = dict(snapshot.metadata or {})
        for key, expected in (
            ("tenant_id", principal.tenant_id),
            ("user_id", principal.user_id),
            ("conversation_id", request.conversation_id or ""),
        ):
            actual = metadata.get(key)
            if actual is not None and actual != expected:
                raise AnalysisRuntimeError(
                    AgentError(
                        code=ErrorCode.ACCESS_DENIED,
                        message="analysis checkpoint owner metadata does not match",
                    )
                )
        return state, request, authority, config

    @staticmethod
    def _validate_resolved(
        request: AgentRequest,
        principal: PrincipalContext,
        resolved: ResolvedAnalysisRun,
    ) -> None:
        authority = resolved.authority
        if (
            authority.tenant_id != principal.tenant_id
            or authority.user_id != principal.user_id
            or authority.mode != request.mode
        ):
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.ACCESS_DENIED,
                    message="resolved dataset authority does not match the request principal",
                )
            )
        if request.source_id is not None and (
            request.source_id != authority.source_id
            or request.source_version != authority.source_version
            or request.binding_id != authority.binding_id
            or request.binding_version != authority.binding_version
        ):
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.BINDING_STALE,
                    message="resolved dataset authority does not match request pins",
                )
            )
        pins = resolved.graph_context.version_pins
        if (
            pins.source_id != authority.source_id
            or pins.source_version != authority.source_version
            or pins.binding_id != authority.binding_id
            or pins.binding_version != authority.binding_version
            or pins.schema_fingerprint != authority.schema_fingerprint
        ):
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.BINDING_STALE,
                    message="runtime version pins do not match dataset authority",
                )
            )

    @staticmethod
    def _config(
        *,
        run_id: str,
        request: AgentRequest,
        principal: PrincipalContext,
        authority: DatasetAuthority,
    ) -> dict[str, object]:
        return {
            "configurable": {"thread_id": run_id},
            "metadata": {
                "tenant_id": principal.tenant_id,
                "user_id": principal.user_id,
                "conversation_id": request.conversation_id or "",
                "source_id": authority.source_id,
                "source_version": authority.source_version,
                "binding_id": authority.binding_id,
                "binding_version": authority.binding_version,
            },
        }

    @staticmethod
    def _started_event(
        *,
        run_id: str,
        request: AgentRequest,
    ) -> AgentEvent:
        return AgentEvent(
            type=AgentEventType.RUN_STARTED,
            run_id=run_id,
            sequence=0,
            data=RunStartedPayload(
                mode=request.mode,
                enterprise_id=request.enterprise_id,
                domain_id=request.domain_id,
            ),
        )

    @staticmethod
    def _failure_event(
        *,
        run_id: str,
        sequence: int,
        request: AgentRequest,
        principal: PrincipalContext,
        code: ErrorCode,
        message: str,
    ) -> AgentEvent:
        error = AgentError(code=code, message=message)
        response = AgentResponse(
            ok=False,
            question=request.question,
            contextualized_question=request.question,
            conversation_id=request.conversation_id,
            tenant_id=principal.tenant_id,
            message_type="error",
            error=error,
        )
        return AgentEvent(
            type=AgentEventType.RUN_FAILED,
            run_id=run_id,
            sequence=sequence,
            data=RunFailedPayload(error_code=code),
            response=response,
        )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("analysis runtime is closed")


__all__ = [
    "AgentResumeRequest",
    "AnalysisRunResolver",
    "AnalysisRuntimeError",
    "DataAnalysisAgentRuntime",
    "ResolvedAnalysisRun",
]
