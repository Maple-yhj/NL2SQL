"""Dependency-injected planning and context boundaries for graph execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from data_agent.runtime.packs import DomainPack
from data_agent.skills.models import LogicalQueryPlan
from data_agent.tools.providers import SemanticMatch

from .contracts import ExecutionContext, ResolvedContext


class ContextResolver(Protocol):
    async def resolve(self, context: ExecutionContext) -> ResolvedContext: ...


class LogicalPlanner(Protocol):
    async def build_plan(
        self,
        *,
        context: ExecutionContext,
        resolved_context: ResolvedContext,
        semantic_matches: tuple[SemanticMatch, ...],
    ) -> LogicalQueryPlan: ...


class ToolInvokerProtocol(Protocol):
    async def invoke(self, call, context): ...


@dataclass(frozen=True, slots=True)
class ExecutionDependencies:
    invoker: ToolInvokerProtocol
    context_resolver: ContextResolver
    planner: LogicalPlanner
    domain_pack: DomainPack
