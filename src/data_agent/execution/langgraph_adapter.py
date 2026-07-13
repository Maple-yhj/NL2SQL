"""Real LangGraph backend for the project-owned GraphSpec IR."""

from __future__ import annotations

import asyncio
from typing import TypedDict

try:
    from langgraph.graph import END, START, StateGraph
    from langgraph.errors import GraphRecursionError
except ImportError:  # pragma: no cover - exercised only by minimal deployments
    END = "__end__"
    START = "__start__"
    StateGraph = None

    class GraphRecursionError(RuntimeError):
        pass

from data_agent.runtime.models import AgentMode

from .contracts import (
    ArtifactKind,
    ExecutionCheckpoint,
    ExecutionContext,
    ExecutionResult,
    ExecutionState,
    ExecutionStatus,
    ExecutionVersionPins,
)
from .dependencies import ExecutionDependencies
from .compiler import graph_static_max_steps
from .executor import (
    CheckpointMismatchError,
    ExecutionFault,
    InternalGraphExecutor,
    _RunFrame,
)
from .models import ErrorCode, GraphSpec


LANGGRAPH_RECURSION_MARGIN = 4


class LangGraphUnavailableError(RuntimeError):
    """Raised instead of silently pretending an unavailable backend exists."""


class _AdapterState(TypedDict):
    execution: ExecutionState
    frame: _RunFrame
    stop_after: str | None
    cursor: str


class LangGraphAdapter:
    """Compile every project GraphSpec node into one real LangGraph StateGraph."""

    def __init__(
        self,
        graph: GraphSpec,
        dependencies: ExecutionDependencies,
    ) -> None:
        if StateGraph is None:
            raise LangGraphUnavailableError(
                "LangGraph is not installed; the adapter cannot be constructed"
            )
        self.graph = graph
        self.dependencies = dependencies
        self._internal = InternalGraphExecutor(graph, dependencies)
        self.static_max_steps = graph_static_max_steps(graph)
        self.recursion_limit = (
            self.static_max_steps + LANGGRAPH_RECURSION_MARGIN
        )
        self.compiled_graph = self._compile_state_graph()

    @property
    def available(self) -> bool:
        return StateGraph is not None

    def _compile_state_graph(self):
        builder = StateGraph(_AdapterState)
        destinations = {node.id: node.id for node in self.graph.nodes}
        destinations[END] = END
        builder.add_conditional_edges(START, self._entry_route, destinations)
        for node in self.graph.nodes:
            builder.add_node(node.id, self._node_runner(node.id))
        for node in self.graph.nodes:
            builder.add_conditional_edges(node.id, self._cursor_route, destinations)
        return builder.compile(name=f"{self.graph.graph_id}@{self.graph.version}")

    @staticmethod
    def _entry_route(state: _AdapterState) -> str:
        return state["execution"].next_node or END

    @staticmethod
    def _cursor_route(state: _AdapterState) -> str:
        return state["cursor"]

    def _node_runner(self, node_id: str):
        async def run(state: _AdapterState) -> dict[str, object]:
            execution = state["execution"]
            frame = state["frame"]
            node = self.graph.node(node_id)
            if node is None:
                raise RuntimeError("compiled LangGraph referenced an unknown spec node")
            execution = execution.model_copy(
                update={
                    "current_node": node.id,
                    "node_trace": (*execution.node_trace, node.id),
                }
            )
            frame.state = execution
            try:
                if execution.mode == AgentMode.PLAN and node.requires_credentials:
                    raise ExecutionFault(
                        ErrorCode.ACCESS_DENIED,
                        "plan mode cannot enter a credential-bearing node",
                    )
                execution = await self._internal._execute_node(node, execution, frame)
            except ExecutionFault as fault:
                execution = fault.state or execution
                try:
                    execution = self._internal._route_error(
                        node,
                        execution,
                        frame,
                        fault,
                    )
                    cursor = execution.next_node
                except ExecutionFault as terminal:
                    execution = self._internal._terminal_error(
                        terminal.state or execution,
                        status=ExecutionStatus.FAILED,
                        code=terminal.code,
                        message=str(terminal),
                        retryable=terminal.retryable,
                    )
                    cursor = END
                frame.state = execution
                return {"execution": execution, "cursor": cursor or END}

            frame.state = execution
            try:
                next_node = (
                    None
                    if node.id in self.graph.terminal_nodes
                    else self._internal._next_node(node.id, execution.mode)
                )
            except ExecutionFault as fault:
                execution = self._internal._terminal_error(
                    execution,
                    status=ExecutionStatus.FAILED,
                    code=fault.code,
                    message=str(fault),
                    retryable=fault.retryable,
                )
                frame.state = execution
                return {"execution": execution, "cursor": END}
            if node.id == state["stop_after"]:
                if next_node is None:
                    raise RuntimeError("cannot checkpoint after a completed graph")
                execution = execution.model_copy(
                    update={
                        "status": ExecutionStatus.PAUSED,
                        "next_node": next_node,
                    }
                )
                cursor = END
            else:
                execution = execution.model_copy(update={"next_node": next_node})
                cursor = next_node or END
            frame.state = execution
            return {"execution": execution, "cursor": cursor}

        return run

    async def execute(self, context: ExecutionContext) -> ExecutionResult:
        self._internal._validate_context(context)
        initial = ExecutionState(
            run_id=context.run_id,
            mode=context.mode,
            status=ExecutionStatus.RUNNING,
            next_node=self.graph.entry_node,
        )
        state = await self._run(context, initial)
        final = state.artifact(ArtifactKind.FINAL)
        if final is None:
            state = self._internal._add_final_artifact(state)
            final = state.require_artifact(ArtifactKind.FINAL)
        return ExecutionResult(state=state, final_artifact=final)

    async def create_checkpoint(
        self,
        context: ExecutionContext,
        *,
        after_node: str,
    ) -> ExecutionCheckpoint:
        self._internal._validate_context(context)
        if self.graph.node(after_node) is None or after_node in self.graph.terminal_nodes:
            raise ValueError("checkpoint node must be a non-terminal graph node")
        initial = ExecutionState(
            run_id=context.run_id,
            mode=context.mode,
            status=ExecutionStatus.RUNNING,
            next_node=self.graph.entry_node,
        )
        state = await self._run(context, initial, stop_after=after_node)
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
        self._internal._validate_context(context)
        self._internal._validate_checkpoint(checkpoint, context)
        initial = checkpoint.state.model_copy(update={"status": ExecutionStatus.RUNNING})
        state = await self._run(context, initial)
        final = state.artifact(ArtifactKind.FINAL)
        if final is None:
            state = self._internal._add_final_artifact(state)
            final = state.require_artifact(ArtifactKind.FINAL)
        return ExecutionResult(state=state, final_artifact=final)

    async def replay(
        self,
        checkpoint: ExecutionCheckpoint,
        context: ExecutionContext,
    ) -> ExecutionResult:
        return await self.resume(checkpoint, context)

    async def _run(
        self,
        context: ExecutionContext,
        initial: ExecutionState,
        *,
        stop_after: str | None = None,
    ) -> ExecutionState:
        frame = _RunFrame(context, initial)
        payload: _AdapterState = {
            "execution": initial,
            "frame": frame,
            "stop_after": stop_after,
            "cursor": initial.next_node or END,
        }
        try:
            async with asyncio.timeout(context.budget.max_duration_seconds):
                output = await self.compiled_graph.ainvoke(
                    payload,
                    config={"recursion_limit": self.recursion_limit},
                )
                return output["execution"]
        except GraphRecursionError:
            return self._internal._terminal_error(
                frame.state,
                status=ExecutionStatus.FAILED,
                code="GRAPH_RECURSION_LIMIT",
                message="LangGraph exhausted the statically derived recursion limit",
            )
        except TimeoutError:
            return self._internal._terminal_error(
                frame.state,
                status=ExecutionStatus.TIMED_OUT,
                code="TIMEOUT",
                message="execution exceeded its duration budget",
            )
        except asyncio.CancelledError:
            return self._internal._terminal_error(
                frame.state,
                status=ExecutionStatus.CANCELLED,
                code="CANCELLED",
                message="execution was cancelled",
            )


__all__ = [
    "CheckpointMismatchError",
    "LANGGRAPH_RECURSION_MARGIN",
    "LangGraphAdapter",
    "LangGraphUnavailableError",
]
