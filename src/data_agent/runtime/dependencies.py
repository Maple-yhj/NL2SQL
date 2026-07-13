"""Dependency-injected runtime ports and immutable composition inputs."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from data_agent.execution import (
    ExecutionCheckpoint,
    ExecutionContext,
    ExecutionResult,
    GraphSpec,
    ResolvedContext,
)
from data_agent.memory import MemoryCandidate, MemoryManager
from data_agent.skills import LogicalQueryPlan, SkillRegistry
from data_agent.tools import ToolRegistry
from data_agent.tools.providers import SemanticMatch

from .bundle_store import BundleSnapshot, BundleStore
from .models import (
    AgentRequest,
    PrincipalContext,
    RuntimeVersionPins,
)


class ModelClient(Protocol):
    model_id: str
    version: str

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str: ...


class LogicalPlanner(Protocol):
    async def build_plan(
        self,
        *,
        context: ExecutionContext,
        resolved_context: ResolvedContext,
        semantic_matches: tuple[SemanticMatch, ...],
    ) -> LogicalQueryPlan: ...


class RuntimeGraphExecutor(Protocol):
    graph: GraphSpec

    async def execute(self, context: ExecutionContext) -> ExecutionResult: ...

    async def create_checkpoint(
        self,
        context: ExecutionContext,
        *,
        after_node: str,
    ) -> ExecutionCheckpoint: ...

    async def resume(
        self,
        checkpoint: ExecutionCheckpoint,
        context: ExecutionContext,
    ) -> ExecutionResult: ...


class MemoryProposalFactory(Protocol):
    async def build(
        self,
        *,
        request: AgentRequest,
        principal: PrincipalContext,
        snapshot: BundleSnapshot,
        result: ExecutionResult,
        run_id: str,
        conversation_id: str,
    ) -> tuple[MemoryCandidate, ...]: ...


class NoMemoryProposals:
    async def build(
        self,
        *,
        request: AgentRequest,
        principal: PrincipalContext,
        snapshot: BundleSnapshot,
        result: ExecutionResult,
        run_id: str,
        conversation_id: str,
    ) -> tuple[MemoryCandidate, ...]:
        return ()


def _run_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    bundle_store: BundleStore
    skill_registry: SkillRegistry
    tool_registry: ToolRegistry
    graph: GraphSpec
    executor: RuntimeGraphExecutor
    memory: MemoryManager
    context_resolver: object
    planner: LogicalPlanner
    model_client: ModelClient
    proposal_factory: MemoryProposalFactory = field(default_factory=NoMemoryProposals)
    resources: tuple[object, ...] = ()
    run_id_factory: Callable[[], str] = _run_id
    deadline_seconds: float | None = None
    checkpoint_after_node: str | None = "validate_logical_plan"

    def __post_init__(self) -> None:
        if self.deadline_seconds is not None and self.deadline_seconds <= 0:
            raise ValueError("runtime deadline must be positive")
        executor_graph = getattr(self.executor, "graph", None)
        if executor_graph is not None and executor_graph != self.graph:
            raise ValueError("runtime executor and graph spec do not match")


__all__ = [
    "LogicalPlanner",
    "MemoryProposalFactory",
    "ModelClient",
    "NoMemoryProposals",
    "RuntimeDependencies",
    "RuntimeGraphExecutor",
    "RuntimeVersionPins",
]
