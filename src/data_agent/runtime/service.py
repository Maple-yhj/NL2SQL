"""Concrete streaming Data Agent application service."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator

from data_agent.execution import (
    ArtifactKind,
    BudgetLimits,
    ExecutionCheckpoint,
    ExecutionContext,
    ExecutionResult,
    ExecutionStatus,
    VersionPin,
)
from data_agent.memory import (
    ArtifactReference,
    CheckpointWrite,
    ConversationRecord,
    ConversationSummaryWrite,
    ConversationWriteBatch,
    MessageRecord,
    MessageRole,
    MessageWrite,
    MemoryCandidate,
    SafeMessagePayload,
    TraceSummary,
)

from .bundle_store import BundleNotActiveError, BundleSnapshot
from .composition import stable_digest
from .context import ContextBudgetExceededError
from .dependencies import RuntimeDependencies
from .errors import AgentError, ErrorCode
from .events import (
    AgentEvent,
    AgentEventType,
    RunCompletedPayload,
    RunFailedPayload,
    RunProgressPayload,
    RunStartedPayload,
)
from .models import (
    AgentMode,
    AgentRequest,
    AgentResponse,
    AgentRow,
    AgentTraceEntry,
    ComponentVersionPin,
    ConversationMessage,
    ConversationMessageMetadata,
    ConversationSummary,
    PrincipalContext,
    ProposalSummary,
    RuntimeVersionPins,
)


_TERMINAL_TYPES = {
    AgentEventType.RUN_COMPLETED,
    AgentEventType.RUN_FAILED,
}


class DefaultDataAgentRuntime:
    """Pin one composition snapshot and stream one safe terminal per run."""

    def __init__(self, dependencies: RuntimeDependencies) -> None:
        self.dependencies = dependencies
        self._state_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._drained = asyncio.Event()
        self._drained.set()
        self._active_runs = 0
        self._closing = False
        self._closed = False

    async def run(
        self,
        request: AgentRequest,
        principal: PrincipalContext,
    ) -> AsyncIterator[AgentEvent]:
        await self._enter_run()
        run_id = self.dependencies.run_id_factory()
        sequence = 0
        resolver_bound = False
        conversation_id = request.conversation_id
        terminal: AgentEvent | None = None
        checkpoint: ExecutionCheckpoint | None = None
        result: ExecutionResult | None = None
        proposals: tuple[MemoryCandidate, ...] = ()
        pins: RuntimeVersionPins | None = None

        try:
            yield AgentEvent(
                type=AgentEventType.RUN_STARTED,
                run_id=run_id,
                sequence=sequence,
                data=RunStartedPayload(
                    mode=request.mode,
                    enterprise_id=request.enterprise_id,
                    domain_id=request.domain_id,
                ),
            )
            sequence += 1

            snapshot = self.dependencies.bundle_store.snapshot()
            self._validate_request_scope(request, snapshot)
            execution_context, pins = self._pin_execution_context(
                request=request,
                principal=principal,
                run_id=run_id,
                snapshot=snapshot,
            )
            yield AgentEvent(
                type=AgentEventType.PROGRESS,
                run_id=run_id,
                sequence=sequence,
                data=RunProgressPayload(pins=pins),
            )
            sequence += 1

            loop = asyncio.get_running_loop()
            duration = (
                self.dependencies.deadline_seconds
                if self.dependencies.deadline_seconds is not None
                else float(execution_context.budget.max_duration_seconds)
            )
            deadline = loop.time() + duration
            try:
                async with asyncio.timeout_at(deadline):
                    conversation_id = await self._resolve_conversation(
                        request=request,
                        principal=principal,
                    )
                    await self._bind_context_resolver(
                        run_id=run_id,
                        conversation_id=conversation_id,
                        request=request,
                        principal=principal,
                        snapshot=snapshot,
                    )
                    resolver_bound = True
                    checkpoint, result = await self._execute_graph(execution_context)
                    proposals = tuple(
                        await self.dependencies.proposal_factory.build(
                            request=request,
                            principal=principal,
                            snapshot=snapshot,
                            result=result,
                            run_id=run_id,
                            conversation_id=conversation_id,
                        )
                    )
                    response, audit_trace = self._response_from_result(
                        request=request,
                        principal=principal,
                        conversation_id=conversation_id,
                        result=result,
                        proposals=proposals,
                        version_pins=pins,
                    )
                    batch = self._turn_batch(
                        request=request,
                        principal=principal,
                        conversation_id=conversation_id,
                        run_id=run_id,
                        result=result,
                        response=response,
                        audit_trace=audit_trace,
                        proposals=proposals,
                        checkpoint=checkpoint,
                    )
                    await self.dependencies.memory.save_turn(batch)

                event_type = (
                    AgentEventType.RUN_COMPLETED
                    if response.ok
                    else AgentEventType.RUN_FAILED
                )
                terminal = self._terminal_event(
                    event_type,
                    run_id,
                    sequence,
                    response,
                )
            except TimeoutError:
                response = self._error_response(
                    request=request,
                    principal=principal,
                    conversation_id=conversation_id,
                    code=ErrorCode.DEADLINE_EXCEEDED,
                    version_pins=pins,
                )
                await self._persist_boundary_failure(
                    request=request,
                    principal=principal,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    response=response,
                )
                terminal = self._terminal_event(
                    AgentEventType.RUN_FAILED,
                    run_id,
                    sequence,
                    response,
                )
            except asyncio.CancelledError:
                response = self._error_response(
                    request=request,
                    principal=principal,
                    conversation_id=conversation_id,
                    code=ErrorCode.CANCELLED,
                    version_pins=pins,
                )
                await self._persist_boundary_failure(
                    request=request,
                    principal=principal,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    response=response,
                )
                terminal = self._terminal_event(
                    AgentEventType.RUN_FAILED,
                    run_id,
                    sequence,
                    response,
                )
            except Exception as exc:
                response = self._error_response(
                    request=request,
                    principal=principal,
                    conversation_id=conversation_id,
                    code=self._boundary_error_code(exc),
                    version_pins=pins,
                )
                terminal = self._terminal_event(
                    AgentEventType.RUN_FAILED,
                    run_id,
                    sequence,
                    response,
                )
        except asyncio.CancelledError:
            response = self._error_response(
                request=request,
                principal=principal,
                conversation_id=conversation_id,
                code=ErrorCode.CANCELLED,
                version_pins=pins,
            )
            terminal = self._terminal_event(
                AgentEventType.RUN_FAILED,
                run_id,
                sequence,
                response,
            )
        except Exception as exc:
            response = self._error_response(
                request=request,
                principal=principal,
                conversation_id=conversation_id,
                code=self._boundary_error_code(exc),
                version_pins=pins,
            )
            terminal = self._terminal_event(
                AgentEventType.RUN_FAILED,
                run_id,
                sequence,
                response,
            )
        finally:
            if resolver_bound:
                await self._unbind_context_resolver(run_id)
            await self._leave_run()

        if terminal is None or terminal.type not in _TERMINAL_TYPES:
            response = self._error_response(
                request=request,
                principal=principal,
                conversation_id=conversation_id,
                code=ErrorCode.INTERNAL_ERROR,
                version_pins=pins,
            )
            terminal = self._terminal_event(
                AgentEventType.RUN_FAILED,
                run_id,
                sequence,
                response,
            )
        yield terminal

    async def create_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        title: str = "",
    ) -> ConversationSummary:
        await self._enter_run()
        try:
            self._validate_conversation_domain(domain_id)
            record = await self.dependencies.memory.create_conversation(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                domain_id=domain_id,
                title=title.strip(),
            )
            return self._conversation_summary(record)
        finally:
            await self._leave_run()

    async def list_conversations(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        limit: int,
        include_archived: bool = False,
    ) -> tuple[ConversationSummary, ...]:
        await self._enter_run()
        try:
            self._validate_conversation_domain(domain_id)
            if limit <= 0:
                raise ValueError("conversation limit must be positive")
            records = await self.dependencies.memory.list_conversations(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                domain_id=domain_id,
                limit=limit,
                include_archived=include_archived,
            )
            return tuple(self._conversation_summary(record) for record in records)
        finally:
            await self._leave_run()

    async def get_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
    ) -> ConversationSummary | None:
        await self._enter_run()
        try:
            self._validate_conversation_domain(domain_id)
            record = await self.dependencies.memory.get_conversation(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                domain_id=domain_id,
                conversation_id=conversation_id,
            )
            return self._conversation_summary(record) if record is not None else None
        finally:
            await self._leave_run()

    async def update_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationSummary | None:
        await self._enter_run()
        try:
            self._validate_conversation_domain(domain_id)
            record = await self.dependencies.memory.update_conversation(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                domain_id=domain_id,
                conversation_id=conversation_id,
                title=title.strip() if title is not None else None,
                archived=archived,
            )
            return self._conversation_summary(record) if record is not None else None
        finally:
            await self._leave_run()

    async def list_conversation_messages(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        await self._enter_run()
        try:
            self._validate_conversation_domain(domain_id)
            if limit <= 0:
                raise ValueError("conversation message limit must be positive")
            conversation = await self.dependencies.memory.get_conversation(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                domain_id=domain_id,
                conversation_id=conversation_id,
            )
            if conversation is None:
                return ()
            records = await self.dependencies.memory.list_messages(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                domain_id=domain_id,
                conversation_id=conversation_id,
                limit=limit,
            )
            return tuple(self._conversation_message(record) for record in records)
        finally:
            await self._leave_run()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            async with self._state_lock:
                self._closing = True
            await self._drained.wait()
            first_error: Exception | None = None
            seen: set[int] = set()
            for resource in reversed(self.dependencies.resources):
                identity = id(resource)
                if identity in seen:
                    continue
                seen.add(identity)
                close = getattr(resource, "close", None)
                if close is None:
                    continue
                try:
                    value = close()
                    if inspect.isawaitable(value):
                        await value
                except Exception as exc:  # finish closing the remaining resources
                    first_error = first_error or exc
            self._closed = True
            if first_error is not None:
                raise first_error

    def _validate_conversation_domain(self, domain_id: str) -> None:
        snapshot = self.dependencies.bundle_store.snapshot()
        if domain_id != snapshot.domain_pack.metadata.name:
            raise ValueError("conversation domain is not available in the active bundle")

    @staticmethod
    def _conversation_summary(record: ConversationRecord) -> ConversationSummary:
        return ConversationSummary(
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            domain_id=record.domain_id,
            conversation_id=record.conversation_id,
            title=record.title,
            archived=record.status.value == "archived",
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _conversation_message(record: MessageRecord) -> ConversationMessage:
        payload = record.payload
        return ConversationMessage(
            role=record.role.value,
            content=record.content,
            metadata=ConversationMessageMetadata(
                message_type=payload.message_type,
                contextualized_question=payload.contextualized_question,
                answer=payload.answer_summary,
                ok=payload.ok,
                error_code=payload.error_code,
                row_count=payload.row_count,
                trace=tuple(
                    AgentTraceEntry(
                        node=item.node,
                        status=item.status,
                        error_code=item.error_code,
                    )
                    for item in payload.trace
                ),
            ),
        )

    async def _enter_run(self) -> None:
        async with self._state_lock:
            if self._closing or self._closed:
                raise RuntimeError("runtime is closed")
            self._active_runs += 1
            self._drained.clear()

    async def _leave_run(self) -> None:
        async with self._state_lock:
            self._active_runs -= 1
            if self._active_runs == 0:
                self._drained.set()

    @staticmethod
    def _validate_request_scope(
        request: AgentRequest,
        snapshot: BundleSnapshot,
    ) -> None:
        if (
            request.enterprise_id != snapshot.enterprise_binding.metadata.name
            or request.domain_id != snapshot.domain_pack.metadata.name
        ):
            raise ValueError("request scope is not available in the active bundle")

    def _pin_execution_context(
        self,
        *,
        request: AgentRequest,
        principal: PrincipalContext,
        run_id: str,
        snapshot: BundleSnapshot,
    ) -> tuple[ExecutionContext, RuntimeVersionPins]:
        skill_id = "commerce.analytics"
        skill_version = snapshot.bundle.skill_versions.get(skill_id)
        skill = self.dependencies.skill_registry.get(skill_id, skill_version)
        if skill_version is None or skill is None:
            raise ValueError("active bundle requests an unavailable skill")
        if self.dependencies.tool_registry.version != snapshot.bundle.tool_registry_version:
            raise ValueError("active bundle and Tool Registry versions do not match")
        allowed_tools = tuple(skill.manifest.allowed_tools)
        specs = {spec.name: spec for spec in self.dependencies.tool_registry.specs()}
        if set(specs) != set(allowed_tools):
            raise ValueError("Tool Registry and selected Skill capabilities drifted")
        tool_versions = tuple(
            VersionPin(component=name, version=specs[name].version)
            for name in allowed_tools
        )
        model_versions = (
            VersionPin(
                component=self.dependencies.model_client.model_id,
                version=self.dependencies.model_client.version,
            ),
        )
        graph = self.dependencies.graph
        runtime_limits = snapshot.bundle.runtime_limits
        budget = BudgetLimits(
            max_correction_rounds=min(
                int(runtime_limits.get("maxCorrectionRounds", graph.limits.max_correction_rounds)),
                graph.limits.max_correction_rounds,
            ),
            max_sql_compile_attempts=min(
                int(runtime_limits.get("maxSqlCompileAttempts", graph.limits.max_sql_compile_attempts)),
                graph.limits.max_sql_compile_attempts,
            ),
            max_tool_calls=min(
                int(runtime_limits.get("maxToolCalls", graph.limits.max_tool_calls)),
                graph.limits.max_tool_calls,
            ),
            max_duration_seconds=min(
                int(runtime_limits.get("maxDurationSeconds", graph.limits.max_duration_seconds)),
                graph.limits.max_duration_seconds,
            ),
            max_result_rows=min(
                int(runtime_limits.get("maxResultRows", graph.limits.max_result_rows)),
                graph.limits.max_result_rows,
            ),
        )
        context = ExecutionContext(
            run_id=run_id,
            mode=request.mode,
            question=request.question,
            enterprise_id=request.enterprise_id,
            domain_id=request.domain_id,
            principal=principal,
            bundle=snapshot.bundle,
            skill_id=skill_id,
            skill_version=skill_version,
            allowed_tools=allowed_tools,
            tool_versions=tool_versions,
            model_versions=model_versions,
            budget=budget,
        )
        pins = RuntimeVersionPins(
            bundle_digest=snapshot.bundle.digest,
            runtime_version=snapshot.bundle.runtime_version,
            domain_pack_digest=snapshot.domain_pack_digest,
            enterprise_binding_digest=snapshot.enterprise_binding_digest,
            deployment_profile_digest=snapshot.deployment_profile_digest,
            schema_fingerprint=snapshot.bundle.schema_fingerprint,
            skill_id=skill_id,
            skill_version=skill_version,
            graph_id=graph.graph_id,
            graph_version=graph.version,
            graph_digest=graph.digest,
            tool_registry_version=snapshot.bundle.tool_registry_version,
            tool_versions=tuple(
                ComponentVersionPin(
                    component=pin.component,
                    version=pin.version,
                )
                for pin in tool_versions
            ),
            model_versions=tuple(
                ComponentVersionPin(
                    component=pin.component,
                    version=pin.version,
                )
                for pin in model_versions
            ),
        )
        return context, pins

    async def _resolve_conversation(
        self,
        *,
        request: AgentRequest,
        principal: PrincipalContext,
    ) -> str:
        if request.conversation_id is None:
            conversation = await self.dependencies.memory.create_conversation(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                domain_id=request.domain_id,
                title=request.question[:120],
            )
            return conversation.conversation_id
        conversation = await self.dependencies.memory.get_conversation(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            domain_id=request.domain_id,
            conversation_id=request.conversation_id,
        )
        if conversation is None:
            raise PermissionError("conversation is unavailable")
        return conversation.conversation_id

    async def _bind_context_resolver(
        self,
        *,
        run_id: str,
        conversation_id: str,
        request: AgentRequest,
        principal: PrincipalContext,
        snapshot: BundleSnapshot,
    ) -> None:
        bind = getattr(self.dependencies.context_resolver, "bind_run", None)
        if bind is None:
            return
        value = bind(
            run_id=run_id,
            conversation_id=conversation_id,
            request=request,
            principal=principal,
            snapshot=snapshot,
        )
        if inspect.isawaitable(value):
            await value

    async def _unbind_context_resolver(self, run_id: str) -> None:
        unbind = getattr(self.dependencies.context_resolver, "unbind_run", None)
        if unbind is None:
            return
        try:
            value = unbind(run_id)
            if inspect.isawaitable(value):
                await value
        except Exception:
            return

    async def _execute_graph(
        self,
        context: ExecutionContext,
    ) -> tuple[ExecutionCheckpoint | None, ExecutionResult]:
        executor = self.dependencies.executor
        checkpoint_node = self.dependencies.checkpoint_after_node
        create = getattr(executor, "create_checkpoint", None)
        resume = getattr(executor, "resume", None)
        if checkpoint_node is not None and create is not None and resume is not None:
            checkpoint = await create(context, after_node=checkpoint_node)
            result = await resume(checkpoint, context)
            return checkpoint, result
        return None, await executor.execute(context)

    def _response_from_result(
        self,
        *,
        request: AgentRequest,
        principal: PrincipalContext,
        conversation_id: str,
        result: ExecutionResult,
        proposals: tuple[MemoryCandidate, ...],
        version_pins: RuntimeVersionPins,
    ) -> tuple[AgentResponse, tuple[TraceSummary, ...]]:
        state = result.state
        if state.run_id != result.final_artifact.payload.model_dump().get(
            "run_id", state.run_id
        ):
            raise ValueError("execution result run identity drifted")
        trace = self._trace_summary(result)
        public_trace = tuple(
            AgentTraceEntry(
                node=item.node,
                status=item.status,
                error_code=item.error_code,
            )
            for item in trace
        )
        if state.status != ExecutionStatus.SUCCEEDED:
            raw_code = state.error.code if state.error is not None else "INTERNAL_ERROR"
            code = self._runtime_error_code(raw_code, state.status)
            return (
                self._error_response(
                    request=request,
                    principal=principal,
                    conversation_id=conversation_id,
                    code=code,
                    retryable=bool(state.error and state.error.retryable),
                    trace=public_trace if request.include_trace else (),
                    version_pins=version_pins,
                ),
                trace,
            )

        plan_artifact = state.artifact(ArtifactKind.LOGICAL_PLAN)
        prepared_artifact = state.artifact(ArtifactKind.PREPARED_QUERY)
        resolved_artifact = state.artifact(ArtifactKind.RESOLVED_CONTEXT)
        data_kind = (
            ArtifactKind.QUERY_RESULT
            if request.mode == AgentMode.EXECUTE
            else ArtifactKind.QUERY_PREVIEW
        )
        data_artifact = state.artifact(data_kind)
        answer_artifact = state.artifact(ArtifactKind.ANSWER)
        data = data_artifact.payload if data_artifact is not None else None
        if data is None:
            rows: tuple[AgentRow, ...] = ()
        else:
            json_data = data.model_dump(mode="json")
            columns = tuple(json_data["columns"])
            rows = tuple(
                AgentRow.model_validate(
                    dict(zip(columns, row["values"], strict=True))
                )
                for row in json_data["rows"]
            )
        answer = answer_artifact.payload.answer if answer_artifact is not None else None
        contextualized = (
            resolved_artifact.payload.contextualized_question
            if resolved_artifact is not None
            else request.question
        )
        pending = tuple(
            ProposalSummary(
                scope=candidate.scope.value,
                source=candidate.source,
            )
            for candidate in proposals
        )
        response = AgentResponse(
            ok=True,
            question=request.question,
            contextualized_question=contextualized,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            logical_plan=plan_artifact.payload if plan_artifact is not None else None,
            sql=(
                prepared_artifact.payload.executable_sql
                if prepared_artifact is not None
                else None
            ),
            message_type="table" if rows else "text",
            rows=rows,
            answer=answer,
            trace=public_trace if request.include_trace else (),
            pending_memory_updates=pending,
            version_pins=version_pins,
        )
        return response, trace

    @staticmethod
    def _trace_summary(result: ExecutionResult) -> tuple[TraceSummary, ...]:
        state = result.state
        traces = [
            TraceSummary(
                node=node,
                status=(
                    state.status.value
                    if index == len(state.node_trace) - 1
                    else "completed"
                ),
                error_code=(
                    state.error.code
                    if state.error is not None and index == len(state.node_trace) - 1
                    else None
                ),
            )
            for index, node in enumerate(state.node_trace)
        ]
        traces.extend(
            TraceSummary(
                node=item.tool_name,
                status=item.status,
                error_code=item.error_code,
            )
            for item in state.tool_trace
        )
        return tuple(traces)

    def _turn_batch(
        self,
        *,
        request: AgentRequest,
        principal: PrincipalContext,
        conversation_id: str,
        run_id: str,
        result: ExecutionResult,
        response: AgentResponse,
        audit_trace: tuple[TraceSummary, ...],
        proposals: tuple[MemoryCandidate, ...],
        checkpoint: ExecutionCheckpoint | None,
    ) -> ConversationWriteBatch:
        references = tuple(
            ArtifactReference(
                artifact_id=artifact.artifact_id,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                domain_id=request.domain_id,
                conversation_id=conversation_id,
                run_id=run_id,
                kind=artifact.kind.value,
                digest=artifact.digest,
                row_count=(
                    result.state.result_rows
                    if artifact.kind in {
                        ArtifactKind.QUERY_PREVIEW,
                        ArtifactKind.QUERY_RESULT,
                    }
                    else None
                ),
            )
            for artifact in result.state.artifacts
        )
        owner = {
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "domain_id": request.domain_id,
            "conversation_id": conversation_id,
            "run_id": run_id,
        }
        assistant_content = response.answer or (
            "Plan prepared." if response.ok else response.error.message
        )
        assistant_payload = SafeMessagePayload(
            message_type=response.message_type,
            contextualized_question=response.contextualized_question,
            answer_summary=response.answer,
            ok=response.ok,
            error_code=response.error.code.value if response.error else None,
            sql_digest=stable_digest(response.sql) if response.sql else None,
            row_count=len(response.rows),
            artifact_refs=references,
            trace=audit_trace,
        )
        checkpoint_write = (
            CheckpointWrite(**owner, checkpoint=checkpoint)
            if checkpoint is not None
            else None
        )
        return ConversationWriteBatch(
            **owner,
            user_message=MessageWrite(
                **owner,
                role=MessageRole.USER,
                content=request.question,
            ),
            assistant_message=MessageWrite(
                **owner,
                role=MessageRole.ASSISTANT,
                content=assistant_content,
                payload=assistant_payload,
            ),
            conversation_summary=ConversationSummaryWrite(
                **owner,
                summary=assistant_content[:1000],
            ),
            artifact_refs=references,
            proposals=proposals,
            checkpoint=checkpoint_write,
        )

    async def _persist_boundary_failure(
        self,
        *,
        request: AgentRequest,
        principal: PrincipalContext,
        conversation_id: str | None,
        run_id: str,
        response: AgentResponse,
    ) -> None:
        if conversation_id is None:
            return
        trace = (
            TraceSummary(
                node="runtime",
                status="failed",
                error_code=response.error.code.value,
            ),
        )
        owner = {
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "domain_id": request.domain_id,
            "conversation_id": conversation_id,
            "run_id": run_id,
        }
        batch = ConversationWriteBatch(
            **owner,
            user_message=MessageWrite(
                **owner,
                role=MessageRole.USER,
                content=request.question,
            ),
            assistant_message=MessageWrite(
                **owner,
                role=MessageRole.ASSISTANT,
                content=response.error.message,
                payload=SafeMessagePayload(
                    ok=False,
                    error_code=response.error.code.value,
                    trace=trace,
                ),
            ),
            conversation_summary=ConversationSummaryWrite(
                **owner,
                summary=response.error.message,
            ),
        )
        try:
            await asyncio.shield(self.dependencies.memory.save_turn(batch))
        except BaseException:
            return

    @staticmethod
    def _runtime_error_code(raw_code: str, status: ExecutionStatus) -> ErrorCode:
        if status == ExecutionStatus.TIMED_OUT:
            return ErrorCode.DEADLINE_EXCEEDED
        if status == ExecutionStatus.CANCELLED:
            return ErrorCode.CANCELLED
        aliases = {
            "TIMEOUT": ErrorCode.DEADLINE_EXCEEDED,
            "POLICY_VIOLATION": ErrorCode.SQL_POLICY_VIOLATION,
            "BUDGET_EXCEEDED": ErrorCode.TOOL_BUDGET_EXCEEDED,
        }
        if raw_code in aliases:
            return aliases[raw_code]
        try:
            return ErrorCode(raw_code)
        except ValueError:
            return ErrorCode.INTERNAL_ERROR

    @staticmethod
    def _boundary_error_code(exc: Exception) -> ErrorCode:
        if isinstance(exc, ContextBudgetExceededError):
            return ErrorCode.CONTEXT_BUDGET_EXCEEDED
        if isinstance(exc, PermissionError):
            return ErrorCode.ACCESS_DENIED
        if isinstance(exc, BundleNotActiveError):
            return ErrorCode.BUNDLE_NOT_FOUND
        if isinstance(exc, ValueError):
            return ErrorCode.CONFIG_INVALID
        return ErrorCode.INTERNAL_ERROR

    @staticmethod
    def _error_response(
        *,
        request: AgentRequest,
        principal: PrincipalContext,
        conversation_id: str | None,
        code: ErrorCode,
        retryable: bool = False,
        trace: tuple[AgentTraceEntry, ...] = (),
        version_pins: RuntimeVersionPins | None = None,
    ) -> AgentResponse:
        messages = {
            ErrorCode.ACCESS_DENIED: "Access denied.",
            ErrorCode.DEADLINE_EXCEEDED: "The run deadline was exceeded.",
            ErrorCode.CANCELLED: "The run was cancelled.",
            ErrorCode.BUNDLE_NOT_FOUND: "No active runtime bundle is available.",
            ErrorCode.CONFIG_INVALID: "The runtime configuration is invalid.",
            ErrorCode.CONTEXT_BUDGET_EXCEEDED: (
                "Mandatory governed context exceeds the runtime context budget."
            ),
        }
        return AgentResponse(
            ok=False,
            question=request.question,
            conversation_id=conversation_id,
            tenant_id=principal.tenant_id,
            error=AgentError(
                code=code,
                message=messages.get(code, "The governed run failed safely."),
                retryable=retryable,
            ),
            trace=trace,
            version_pins=version_pins,
        )

    @staticmethod
    def _terminal_event(
        event_type: AgentEventType,
        run_id: str,
        sequence: int,
        response: AgentResponse,
    ) -> AgentEvent:
        if event_type == AgentEventType.RUN_COMPLETED:
            payload = RunCompletedPayload()
        elif event_type == AgentEventType.RUN_FAILED:
            if response.error is None:
                raise ValueError("failed event response requires an error")
            payload = RunFailedPayload(error_code=response.error.code)
        else:
            raise ValueError("terminal event requires a terminal event type")
        return AgentEvent(
            type=event_type,
            run_id=run_id,
            sequence=sequence,
            data=payload,
            response=response,
        )


DataAgentRuntimeService = DefaultDataAgentRuntime


__all__ = ["DataAgentRuntimeService", "DefaultDataAgentRuntime"]
