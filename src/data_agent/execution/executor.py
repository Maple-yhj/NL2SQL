"""Backend-independent node handlers and the internal graph executor."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from data_agent.runtime.models import AgentMode
from data_agent.skills import CommerceAnalyticsSkill
from data_agent.tools import (
    ToolBudget,
    ToolCall,
    ToolErrorCode,
    ToolInvocationContext,
    ToolResult,
)
from data_agent.tools.providers import (
    AnswerRenderInput,
    AnswerRenderOutput,
    DataInspectInput,
    DataInspectOutput,
    QueryCompileInput,
    QueryCompileOutput,
    QueryData,
    QueryExecuteInput,
    QueryExecutionOutput,
    QueryMode,
    ResultProfileInput,
    ResultProfileOutput,
    SemanticSearchInput,
    SemanticSearchOutput,
)

from .contracts import (
    Artifact,
    ExecutionCheckpoint,
    EvidenceValidation,
    ExecutionContext,
    ExecutionError,
    ExecutionResult,
    ExecutionState,
    ExecutionStatus,
    ExecutionToolTrace,
    ExecutionVersionPins,
    FinalOutput,
    ResolvedContext,
    RouteAttempt,
    StaticQueryValidation,
)
from .dependencies import ExecutionDependencies
from .models import (
    ArtifactKind,
    EdgeCondition,
    ErrorBudget,
    ErrorCode,
    GraphSpec,
    NodeSpec,
    edge_condition_matches,
)


class ExecutionFault(RuntimeError):
    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        retryable: bool = False,
        state: ExecutionState | None = None,
    ) -> None:
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.retryable = retryable
        self.state = state
        super().__init__(message)


class CheckpointMismatchError(ValueError):
    """Raised when replay authority or artifact integrity has drifted."""


class _RunFrame:
    __slots__ = ("context", "tool_context", "state")

    def __init__(self, context: ExecutionContext, state: ExecutionState) -> None:
        self.context = context
        self.state = state
        remaining_calls = context.budget.max_tool_calls - state.tool_calls
        self.tool_context = ToolInvocationContext(
            principal=context.principal,
            skill_id=context.skill_id,
            skill_version=context.skill_version,
            allowed_tools=context.allowed_tools,
            bundle=context.bundle,
            budget=ToolBudget(max_calls=max(1, remaining_calls)),
        )


class InternalGraphExecutor:
    """Execute a compiled GraphSpec without exposing backend-specific state."""

    def __init__(
        self,
        graph: GraphSpec,
        dependencies: ExecutionDependencies,
    ) -> None:
        self.graph = graph
        self.dependencies = dependencies
        self._skill = CommerceAnalyticsSkill()

    async def execute(self, context: ExecutionContext) -> ExecutionResult:
        self._validate_context(context)
        state = ExecutionState(
            run_id=context.run_id,
            mode=context.mode,
            status=ExecutionStatus.RUNNING,
            next_node=self.graph.entry_node,
        )
        return await self._complete(context, state)

    async def create_checkpoint(
        self,
        context: ExecutionContext,
        *,
        after_node: str,
    ) -> ExecutionCheckpoint:
        self._validate_context(context)
        if self.graph.node(after_node) is None or after_node in self.graph.terminal_nodes:
            raise ValueError("checkpoint node must be a non-terminal graph node")
        state = ExecutionState(
            run_id=context.run_id,
            mode=context.mode,
            status=ExecutionStatus.RUNNING,
            next_node=self.graph.entry_node,
        )
        frame = _RunFrame(context, state)
        try:
            async with asyncio.timeout(context.budget.max_duration_seconds):
                state = await self._drive(state, frame, stop_after=after_node)
        except (TimeoutError, asyncio.CancelledError, ExecutionFault) as exc:
            raise RuntimeError("execution did not reach the checkpoint boundary") from exc
        if state.status != ExecutionStatus.PAUSED:
            raise RuntimeError("execution ended before the checkpoint boundary")
        return ExecutionCheckpoint.capture(
            pins=ExecutionVersionPins.for_run(context, self.graph),
            state=state,
        )

    async def resume(
        self,
        checkpoint: ExecutionCheckpoint,
        context: ExecutionContext,
    ) -> ExecutionResult:
        self._validate_context(context)
        self._validate_checkpoint(checkpoint, context)
        state = checkpoint.state.model_copy(update={"status": ExecutionStatus.RUNNING})
        return await self._complete(context, state)

    async def replay(
        self,
        checkpoint: ExecutionCheckpoint,
        context: ExecutionContext,
    ) -> ExecutionResult:
        return await self.resume(checkpoint, context)

    async def _complete(
        self,
        context: ExecutionContext,
        state: ExecutionState,
    ) -> ExecutionResult:
        frame = _RunFrame(context, state)
        try:
            async with asyncio.timeout(context.budget.max_duration_seconds):
                state = await self._drive(state, frame)
        except TimeoutError:
            state = frame.state
            state = self._terminal_error(
                state,
                status=ExecutionStatus.TIMED_OUT,
                code="TIMEOUT",
                message="execution exceeded its duration budget",
            )
        except asyncio.CancelledError:
            state = frame.state
            state = self._terminal_error(
                state,
                status=ExecutionStatus.CANCELLED,
                code="CANCELLED",
                message="execution was cancelled",
            )
        except ExecutionFault as fault:
            state = fault.state or frame.state
            state = self._terminal_error(
                state,
                status=ExecutionStatus.FAILED,
                code=fault.code,
                message=str(fault),
                retryable=fault.retryable,
            )
        final = state.artifact(ArtifactKind.FINAL)
        if final is None:
            state = self._add_final_artifact(state)
            final = state.require_artifact(ArtifactKind.FINAL)
        return ExecutionResult(state=state, final_artifact=final)

    async def _drive(
        self,
        state: ExecutionState,
        frame: _RunFrame,
        *,
        stop_after: str | None = None,
    ) -> ExecutionState:
        frame.state = state
        while state.next_node is not None:
            node = self.graph.node(state.next_node)
            if node is None:
                raise RuntimeError("compiled graph referenced a missing node")
            if state.mode == AgentMode.PLAN and node.requires_credentials:
                raise ExecutionFault(
                    ErrorCode.ACCESS_DENIED,
                    "plan mode cannot enter a credential-bearing node",
                )
            state = state.model_copy(
                update={
                    "current_node": node.id,
                    "node_trace": (*state.node_trace, node.id),
                }
            )
            frame.state = state
            try:
                state = await self._execute_node(node, state, frame)
            except ExecutionFault as fault:
                state = self._route_error(node, fault.state or state, frame, fault)
                frame.state = state
                continue
            frame.state = state
            next_node = (
                None
                if node.id in self.graph.terminal_nodes
                else self._next_node(node.id, state.mode)
            )
            if node.id == stop_after:
                if next_node is None:
                    raise RuntimeError("cannot checkpoint after a completed graph")
                state = state.model_copy(
                    update={
                        "status": ExecutionStatus.PAUSED,
                        "next_node": next_node,
                    }
                )
                frame.state = state
                return state
            state = state.model_copy(update={"next_node": next_node})
            frame.state = state
            if node.id in self.graph.terminal_nodes:
                return state
        return state

    def _route_error(
        self,
        node: NodeSpec,
        state: ExecutionState,
        frame: _RunFrame,
        fault: ExecutionFault,
    ) -> ExecutionState:
        try:
            code = ErrorCode(fault.code)
        except ValueError:
            fault.state = state
            raise fault
        route = next((item for item in node.on_error if item.code == code), None)
        if (
            route is None
            or route.terminal
            or not fault.retryable
            or state.mode not in route.allowed_modes
        ):
            fault.state = state
            raise fault

        previous = next(
            (
                item
                for item in state.route_attempts
                if item.node_id == node.id and item.error_code == code.value
            ),
            None,
        )
        attempts = previous.attempts if previous is not None else 0
        if route.max_attempts is None or attempts >= route.max_attempts:
            fault.state = state
            raise fault

        if (
            route.budget == ErrorBudget.SQL_COMPILE
            and state.sql_compile_attempts
            >= frame.context.budget.max_sql_compile_attempts
        ):
            fault.state = state
            raise fault

        correction_rounds = state.correction_rounds
        if route.budget in {ErrorBudget.CORRECTION, ErrorBudget.DIAGNOSTIC}:
            if correction_rounds >= frame.context.budget.max_correction_rounds:
                fault.state = state
                raise fault
            correction_rounds += 1

        updated_attempt = RouteAttempt(
            node_id=node.id,
            error_code=code.value,
            attempts=attempts + 1,
        )
        retained = tuple(
            item
            for item in state.route_attempts
            if not (item.node_id == node.id and item.error_code == code.value)
        )
        return state.model_copy(
            update={
                "next_node": route.target,
                "correction_rounds": correction_rounds,
                "route_attempts": (*retained, updated_attempt),
            }
        )

    async def _execute_node(
        self,
        node: NodeSpec,
        state: ExecutionState,
        frame: _RunFrame,
    ) -> ExecutionState:
        handlers = {
            "resolve_context": self._resolve_context,
            "semantic_search": self._semantic_search,
            "build_logical_plan": self._build_logical_plan,
            "validate_logical_plan": self._validate_logical_plan,
            "inspect_binding": self._inspect_binding,
            "compile_query": self._compile_query,
            "validate_query": self._validate_query,
            "explain_cost": self._explain_cost,
            "execute_preview": self._execute_preview,
            "validate_preview": self._validate_preview,
            "execute_query": self._execute_query,
            "profile_result": self._profile_result,
            "validate_result": self._validate_result,
            "render_answer": self._render_answer,
            "finalize": self._finalize,
        }
        handler = handlers.get(node.id)
        if handler is None:
            raise RuntimeError(f"no node handler is registered for {node.id}")
        return await handler(node, state, frame)

    async def _resolve_context(self, node, state, frame):
        resolved = await self.dependencies.context_resolver.resolve(frame.context)
        if not isinstance(resolved, ResolvedContext):
            raise TypeError("context resolver must return ResolvedContext")
        return self._put(state, node, ArtifactKind.RESOLVED_CONTEXT, resolved)

    async def _semantic_search(self, node, state, frame):
        resolved = state.require_artifact(ArtifactKind.RESOLVED_CONTEXT).payload
        result, state = await self._invoke(
            node,
            SemanticSearchInput(query=resolved.contextualized_question),
            state,
            frame,
        )
        output = self._typed(result, SemanticSearchOutput)
        return self._put(state, node, ArtifactKind.SEMANTIC_MATCHES, output)

    async def _build_logical_plan(self, node, state, frame):
        resolved = state.require_artifact(ArtifactKind.RESOLVED_CONTEXT).payload
        semantic = state.require_artifact(ArtifactKind.SEMANTIC_MATCHES).payload
        plan = await self.dependencies.planner.build_plan(
            context=frame.context,
            resolved_context=resolved,
            semantic_matches=semantic.matches,
        )
        return self._put(state, node, ArtifactKind.LOGICAL_PLAN, plan)

    async def _validate_logical_plan(self, node, state, frame):
        plan = state.require_artifact(ArtifactKind.LOGICAL_PLAN).payload
        validation = self._skill.validate_plan(plan, self.dependencies.domain_pack)
        state = self._put(
            state,
            node,
            ArtifactKind.PLAN_VALIDATION,
            validation,
        )
        if not validation.valid:
            raise ExecutionFault(
                ErrorCode.LOGICAL_PLAN_INVALID,
                "logical plan failed deterministic validation",
                retryable=True,
            )
        return state

    async def _inspect_binding(self, node, state, frame):
        result, state = await self._invoke(node, DataInspectInput(), state, frame)
        output = self._typed(result, DataInspectOutput)
        return self._put(
            state,
            node,
            ArtifactKind.CATALOG_SNAPSHOT,
            output.catalog,
        )

    async def _compile_query(self, node, state, frame):
        if state.sql_compile_attempts >= frame.context.budget.max_sql_compile_attempts:
            raise ExecutionFault(
                ErrorCode.SQL_COMPILE_ERROR,
                "SQL compile attempt budget is exhausted",
                state=state,
            )
        state = state.model_copy(
            update={"sql_compile_attempts": state.sql_compile_attempts + 1}
        )
        plan = state.require_artifact(ArtifactKind.LOGICAL_PLAN).payload
        result, state = await self._invoke(
            node,
            QueryCompileInput(logical_plan=plan),
            state,
            frame,
        )
        output = self._typed(result, QueryCompileOutput)
        prepared = output.prepared_query
        if result.policy_decision_id != prepared.policy_decision_id:
            raise ExecutionFault(
                ErrorCode.SQL_POLICY_VIOLATION,
                "compiled query policy identity drifted",
            )
        state = self._put(
            state,
            node,
            ArtifactKind.BOUND_QUERY_PLAN,
            output.bound_plan,
        )
        return self._put(
            state,
            node,
            ArtifactKind.PREPARED_QUERY,
            prepared,
        )

    async def _validate_query(self, node, state, frame):
        context = frame.context
        plan = state.require_artifact(ArtifactKind.LOGICAL_PLAN).payload
        prepared = state.require_artifact(ArtifactKind.PREPARED_QUERY).payload
        allowlist = set(context.bundle.compiled_access_policy.get("relationAllowlist", ()))
        max_rows = min(
            context.budget.max_result_rows,
            int(context.bundle.runtime_limits.get("maxResultRows", 1000)),
        )
        valid = (
            prepared.logical_plan_hash == plan.stable_hash()
            and prepared.logical_plan == plan
            and prepared.bundle_digest == context.bundle.digest
            and prepared.schema_fingerprint == context.bundle.schema_fingerprint
            and set(prepared.allowed_relations).issubset(allowlist)
            and prepared.max_rows <= max_rows
            and prepared.read_only is True
        )
        if not valid:
            raise ExecutionFault(
                ErrorCode.SQL_POLICY_VIOLATION,
                "prepared query failed static policy validation",
            )
        validation = StaticQueryValidation(
            valid=True,
            logical_plan_hash=prepared.logical_plan_hash,
            query_hash=prepared.sql_ast_hash,
            policy_decision_id=prepared.policy_decision_id,
            bundle_digest=prepared.bundle_digest,
            schema_fingerprint=prepared.schema_fingerprint,
        )
        return self._put(
            state,
            node,
            ArtifactKind.STATIC_VALIDATION,
            validation,
        )

    async def _explain_cost(self, node, state, frame):
        prepared = state.require_artifact(ArtifactKind.PREPARED_QUERY).payload
        result, state = await self._invoke(
            node,
            QueryExecuteInput(
                prepared_query=prepared,
                mode=QueryMode.EXPLAIN,
                preview_rows=frame.context.preview_rows,
            ),
            state,
            frame,
        )
        output = self._typed(result, QueryExecutionOutput)
        if output.mode != QueryMode.EXPLAIN or output.explain is None:
            raise ExecutionFault(
                "TOOL_OUTPUT_INVALID",
                "EXPLAIN returned invalid evidence",
                state=state,
            )
        estimated_cost = output.explain.estimated_cost
        if (
            frame.context.max_estimated_cost is not None
            and estimated_cost is not None
            and estimated_cost > frame.context.max_estimated_cost
        ):
            raise ExecutionFault(
                ErrorCode.COST_EXCEEDED,
                "estimated query cost exceeds the governed threshold",
                retryable=True,
                state=state,
            )
        return self._put(
            state,
            node,
            ArtifactKind.EXPLAIN_RESULT,
            output.explain,
        )

    async def _execute_preview(self, node, state, frame):
        output, state = await self._query_execute(
            node,
            state,
            frame,
            QueryMode.PREVIEW,
        )
        return self._put(
            state,
            node,
            ArtifactKind.QUERY_PREVIEW,
            output.data,
        )

    async def _validate_preview(self, node, state, frame):
        data = state.require_artifact(ArtifactKind.QUERY_PREVIEW).payload
        self._validate_query_data(data, state, frame.context)
        if not data.rows:
            raise ExecutionFault(
                ErrorCode.EMPTY_RESULT,
                "preview returned no rows",
                retryable=True,
            )
        validation = self._evidence_validation("preview", data)
        return self._put(
            state,
            node,
            ArtifactKind.PREVIEW_VALIDATION,
            validation,
        )

    async def _execute_query(self, node, state, frame):
        output, state = await self._query_execute(
            node,
            state,
            frame,
            QueryMode.EXECUTE,
        )
        return self._put(
            state,
            node,
            ArtifactKind.QUERY_RESULT,
            output.data,
        )

    async def _query_execute(self, node, state, frame, mode):
        prepared = state.require_artifact(ArtifactKind.PREPARED_QUERY).payload
        result, state = await self._invoke(
            node,
            QueryExecuteInput(
                prepared_query=prepared,
                mode=mode,
                preview_rows=frame.context.preview_rows,
            ),
            state,
            frame,
        )
        output = self._typed(result, QueryExecutionOutput)
        if output.mode != mode or output.data is None:
            raise ExecutionFault(
                "TOOL_OUTPUT_INVALID",
                f"{mode.value} returned invalid evidence",
                state=state,
            )
        self._validate_query_data(output.data, state, frame.context)
        return output, state

    async def _profile_result(self, node, state, frame):
        kind = (
            ArtifactKind.QUERY_RESULT
            if state.mode == AgentMode.EXECUTE
            else ArtifactKind.QUERY_PREVIEW
        )
        data = state.require_artifact(kind).payload
        result, state = await self._invoke(
            node,
            ResultProfileInput(data=data),
            state,
            frame,
        )
        output = self._typed(result, ResultProfileOutput)
        return self._put(state, node, ArtifactKind.RESULT_PROFILE, output)

    async def _validate_result(self, node, state, frame):
        data_kind = (
            ArtifactKind.QUERY_RESULT
            if state.mode == AgentMode.EXECUTE
            else ArtifactKind.QUERY_PREVIEW
        )
        data = state.require_artifact(data_kind).payload
        profile = state.require_artifact(ArtifactKind.RESULT_PROFILE).payload
        self._validate_query_data(data, state, frame.context)
        valid = (
            profile.logical_plan_hash == data.logical_plan_hash
            and profile.query_hash == data.query_hash
            and profile.policy_decision_id == data.policy_decision_id
            and profile.row_count == len(data.rows)
            and profile.row_count <= frame.context.budget.max_result_rows
        )
        if not valid:
            raise ExecutionFault(
                ErrorCode.RESULT_SEMANTIC_MISMATCH,
                "profile does not match governed query evidence",
                retryable=True,
            )
        validation = self._evidence_validation("result", data)
        state = state.model_copy(update={"result_rows": len(data.rows)})
        return self._put(
            state,
            node,
            ArtifactKind.RESULT_VALIDATION,
            validation,
        )

    async def _render_answer(self, node, state, frame):
        data_kind = (
            ArtifactKind.QUERY_RESULT
            if state.mode == AgentMode.EXECUTE
            else ArtifactKind.QUERY_PREVIEW
        )
        data = state.require_artifact(data_kind).payload
        profile = state.require_artifact(ArtifactKind.RESULT_PROFILE).payload
        result, state = await self._invoke(
            node,
            AnswerRenderInput(
                question=frame.context.question,
                data=data,
                profile=profile,
            ),
            state,
            frame,
        )
        output = self._typed(result, AnswerRenderOutput)
        return self._put(state, node, ArtifactKind.ANSWER, output)

    async def _finalize(self, node, state, frame):
        state = state.model_copy(update={"status": ExecutionStatus.SUCCEEDED})
        return self._add_final_artifact(state, producing_node=node.id)

    async def _invoke(self, node, payload, state, frame):
        if state.tool_calls >= frame.context.budget.max_tool_calls:
            raise ExecutionFault(
                "BUDGET_EXCEEDED",
                "tool call budget is exhausted",
                state=state,
            )
        call_number = state.tool_calls + 1
        call = ToolCall(
            call_id=f"{frame.context.run_id}:{call_number}:{node.id}",
            tool_name=node.tool_ref,
            tool_version=frame.context.tool_version(node.tool_ref),
            input_data=payload,
        )
        result = await self.dependencies.invoker.invoke(call, frame.tool_context)
        state = state.model_copy(
            update={
                "tool_calls": call_number,
                "tool_trace": (
                    *state.tool_trace,
                    ExecutionToolTrace(
                        call_id=result.redacted_trace.call_id,
                        tool_name=result.redacted_trace.tool_name,
                        tool_version=result.redacted_trace.tool_version,
                        status=result.redacted_trace.status,
                        attempts=result.redacted_trace.attempts,
                        error_code=(
                            result.redacted_trace.error_code.value
                            if result.redacted_trace.error_code is not None
                            else None
                        ),
                    ),
                ),
            }
        )
        if result.status == "error":
            error = result.structured_error
            code, retryable = self._map_tool_error(node.id, error.code, error.retryable)
            raise ExecutionFault(
                code,
                "governed tool invocation failed",
                retryable=retryable,
                state=state,
            )
        return result, state

    @staticmethod
    def _typed(result: ToolResult, expected_type: type[BaseModel]):
        if not isinstance(result.typed_data, expected_type):
            raise ExecutionFault("TOOL_OUTPUT_INVALID", "tool returned the wrong typed output")
        return result.typed_data

    @staticmethod
    def _map_tool_error(node_id: str, code: ToolErrorCode, retryable: bool):
        if code in {
            ToolErrorCode.ACCESS_DENIED,
            ToolErrorCode.TOOL_NOT_ALLOWED,
            ToolErrorCode.GRANT_INVALID,
            ToolErrorCode.GRANT_EXPIRED,
            ToolErrorCode.CREDENTIAL_UNAVAILABLE,
        }:
            return ErrorCode.ACCESS_DENIED, False
        if code in {ToolErrorCode.POLICY_VIOLATION, ToolErrorCode.RELATION_NOT_ALLOWED}:
            return ErrorCode.SQL_POLICY_VIOLATION, retryable
        if code == ToolErrorCode.BINDING_STALE:
            return ErrorCode.BINDING_STALE, retryable
        if code == ToolErrorCode.SQL_COMPILE_ERROR:
            return ErrorCode.SQL_COMPILE_ERROR, retryable
        if code == ToolErrorCode.LOGICAL_PLAN_INVALID:
            return ErrorCode.LOGICAL_PLAN_INVALID, retryable
        if code == ToolErrorCode.ROW_LIMIT_EXCEEDED:
            return ErrorCode.JOIN_EXPLOSION, retryable
        if node_id == "compile_query":
            return ErrorCode.SQL_COMPILE_ERROR, retryable
        return code.value, retryable

    @staticmethod
    def _validate_query_data(data: QueryData, state, context):
        prepared = state.require_artifact(ArtifactKind.PREPARED_QUERY).payload
        if len(data.rows) > context.budget.max_result_rows:
            raise ExecutionFault(
                "ROW_LIMIT_EXCEEDED",
                "query evidence exceeds the run row budget",
                state=state,
            )
        if (
            data.logical_plan_hash != prepared.logical_plan_hash
            or data.query_hash != prepared.sql_ast_hash
            or data.policy_decision_id != prepared.policy_decision_id
            or len(data.rows) > context.budget.max_result_rows
        ):
            raise ExecutionFault(
                ErrorCode.RESULT_SEMANTIC_MISMATCH,
                "query evidence identity does not match the prepared query",
                state=state,
            )

    @staticmethod
    def _evidence_validation(stage: str, data: QueryData) -> EvidenceValidation:
        return EvidenceValidation(
            stage=stage,
            valid=True,
            logical_plan_hash=data.logical_plan_hash,
            query_hash=data.query_hash,
            policy_decision_id=data.policy_decision_id,
            row_count=len(data.rows),
        )

    @staticmethod
    def _put(state, node, kind, payload):
        artifact = Artifact.create(
            kind=kind,
            producing_node=node.id,
            payload=payload,
        )
        retained = tuple(item for item in state.artifacts if item.kind != kind)
        return state.model_copy(update={"artifacts": (*retained, artifact)})

    def _next_node(self, source: str, mode: AgentMode) -> str | None:
        matches = [
            edge.target
            for edge in self.graph.edges
            if edge.source == source and self._condition_matches(edge.condition, mode)
        ]
        if len(matches) != 1:
            raise ExecutionFault(
                "GRAPH_ROUTING_ERROR",
                f"expected exactly one graph edge after {source}; got {len(matches)}",
            )
        return matches[0]

    @staticmethod
    def _condition_matches(condition: EdgeCondition, mode: AgentMode) -> bool:
        return edge_condition_matches(condition, mode)

    def _validate_context(self, context: ExecutionContext) -> None:
        if context.budget != self.graph.limits:
            for field_name in type(self.graph.limits).model_fields:
                if getattr(context.budget, field_name) > getattr(
                    self.graph.limits,
                    field_name,
                ):
                    raise ValueError("run budget exceeds compiled graph limits")

    def _validate_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        context: ExecutionContext,
    ) -> None:
        integrity_error = checkpoint.integrity_error()
        if integrity_error is not None:
            raise CheckpointMismatchError(integrity_error)
        expected = ExecutionVersionPins.for_run(context, self.graph)
        if checkpoint.pins != expected:
            raise CheckpointMismatchError("checkpoint version pins do not match the run")
        if checkpoint.state.run_id != context.run_id:
            raise CheckpointMismatchError("checkpoint run id does not match the run")
        if checkpoint.state.mode != context.mode:
            raise CheckpointMismatchError("checkpoint mode does not match the run")

    def _terminal_error(
        self,
        state: ExecutionState,
        *,
        status: ExecutionStatus,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> ExecutionState:
        state = state.model_copy(
            update={
                "status": status,
                "next_node": None,
                "error": ExecutionError(
                    code=code,
                    message=message,
                    node_id=state.current_node,
                    retryable=retryable,
                ),
            }
        )
        return self._add_final_artifact(state)

    def _add_final_artifact(
        self,
        state: ExecutionState,
        *,
        producing_node: str | None = None,
    ) -> ExecutionState:
        plan_artifact = state.artifact(ArtifactKind.LOGICAL_PLAN)
        prepared_artifact = state.artifact(ArtifactKind.PREPARED_QUERY)
        answer_artifact = state.artifact(ArtifactKind.ANSWER)
        plan = plan_artifact.payload if plan_artifact else None
        prepared = prepared_artifact.payload if prepared_artifact else None
        answer = answer_artifact.payload if answer_artifact else None
        payload = FinalOutput(
            status=state.status,
            mode=state.mode,
            logical_plan_hash=plan.stable_hash() if plan is not None else None,
            query_hash=prepared.sql_ast_hash if prepared is not None else None,
            policy_decision_id=(
                prepared.policy_decision_id if prepared is not None else None
            ),
            row_count=state.result_rows,
            answer=answer.answer if answer is not None else None,
            error_code=state.error.code if state.error is not None else None,
            artifact_digests=tuple(item.digest for item in state.artifacts),
        )
        node_id = producing_node or state.current_node or "finalize"
        return self._put(state, NodeSpec(id=node_id, kind="terminal"), ArtifactKind.FINAL, payload)
