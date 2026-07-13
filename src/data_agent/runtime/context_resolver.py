"""Execution-graph callback that recalls memory then calls the pure assembler."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from data_agent.execution import ExecutionContext, ResolvedContext
from data_agent.memory import (
    ConversationMemoryContent,
    EnterpriseMemoryContent,
    EpisodicMemoryContent,
    MemoryBudget,
    MemoryQuery,
    MemoryScope,
    UserMemoryContent,
    WorkingMemoryContent,
)

from .bundle_store import BundleSnapshot
from .context import (
    ContextAssembler,
    ContextBudget,
    ContextItem,
    ContextOwner,
    ContextSource,
    ContextVersionPins,
    SecurityContext,
)
from .models import AgentRequest, PrincipalContext


@dataclass(frozen=True, slots=True)
class _RunSeed:
    request: AgentRequest
    principal: PrincipalContext
    snapshot: BundleSnapshot
    conversation_id: str


class RuntimeContextResolver:
    """Own all memory I/O at the graph boundary, never in ContextAssembler."""

    def __init__(
        self,
        *,
        memory: Any,
        assembler: ContextAssembler,
        memory_budget: MemoryBudget | None = None,
        context_budget: ContextBudget | None = None,
    ) -> None:
        self._memory = memory
        self._assembler = assembler
        self._memory_budget = memory_budget or MemoryBudget()
        self._context_budget = context_budget or ContextBudget()
        self._runs: dict[str, _RunSeed] = {}
        self._lock = asyncio.Lock()

    async def bind_run(
        self,
        *,
        run_id: str,
        conversation_id: str,
        request: AgentRequest,
        principal: PrincipalContext,
        snapshot: BundleSnapshot,
        **_: Any,
    ) -> None:
        seed = _RunSeed(
            request=request,
            principal=principal,
            snapshot=snapshot,
            conversation_id=conversation_id,
        )
        async with self._lock:
            if run_id in self._runs:
                raise ValueError("run context is already bound")
            self._runs[run_id] = seed

    async def unbind_run(self, run_id: str) -> None:
        async with self._lock:
            self._runs.pop(run_id, None)

    async def resolve(self, context: ExecutionContext) -> ResolvedContext:
        async with self._lock:
            seed = self._runs.get(context.run_id)
        if seed is None:
            raise RuntimeError("execution context was not bound by the runtime")
        if (
            seed.principal != context.principal
            or seed.request.mode != context.mode
            or seed.snapshot.bundle.digest != context.bundle.digest
        ):
            raise PermissionError("bound run context does not match execution authority")

        pins = ContextVersionPins(
            domain_version=seed.snapshot.domain_pack.metadata.version,
            binding_version=seed.snapshot.enterprise_binding.metadata.version,
            skill_version=context.skill_version,
            schema_fingerprint=context.bundle.schema_fingerprint,
        )
        now = datetime.now(UTC)
        memory_bundle = await self._memory.recall(
            MemoryQuery(
                tenant_id=context.principal.tenant_id,
                user_id=context.principal.user_id,
                domain_id=seed.request.domain_id,
                conversation_id=seed.conversation_id,
                scopes=(
                    MemoryScope.CONVERSATION,
                    MemoryScope.USER,
                    MemoryScope.EPISODIC,
                    MemoryScope.ENTERPRISE,
                ),
                query=context.question,
                versions=self._memory_versions(pins),
                as_of=now,
            ),
            self._memory_budget,
        )
        conversation = await self._memory.get_conversation(
            tenant_id=context.principal.tenant_id,
            user_id=context.principal.user_id,
            domain_id=seed.request.domain_id,
            conversation_id=seed.conversation_id,
        )
        if conversation is None:
            raise PermissionError("conversation authority changed during the run")

        security = self._security_context(context, now)
        items = [
            *self._domain_items(seed.snapshot, context.question, now),
            *self._binding_items(seed.snapshot, now),
            *self._skill_items(context, now),
            *self._memory_items(memory_bundle.records, context.question),
        ]
        if conversation.summary:
            items.append(
                ContextItem(
                    source=ContextSource.CONVERSATION,
                    key="conversation.summary",
                    content=conversation.summary,
                    version=conversation.updated_at.isoformat(),
                    trust_level="high",
                    sensitivity="internal",
                    token_cost=self._token_cost(conversation.summary),
                    valid_from=conversation.created_at,
                    owner=ContextOwner(
                        tenant_id=context.principal.tenant_id,
                        user_id=context.principal.user_id,
                        domain_id=seed.request.domain_id,
                        conversation_id=seed.conversation_id,
                    ),
                    approved=True,
                    version_pins=pins,
                    relevance=1.0,
                )
            )
        envelope = self._assembler.assemble(
            security_context=security,
            items=tuple(items),
            pins=pins,
            budget=self._context_budget,
            now=now,
            domain_id=seed.request.domain_id,
            conversation_id=seed.conversation_id,
            run_id=context.run_id,
        )
        approved = tuple(
            item.content
            for item in (
                *envelope.approved_enterprise_memory_context,
                *envelope.user_memory_context,
                *envelope.conversation_context,
            )
            if item.key != "conversation.summary"
        )
        return ResolvedContext(
            contextualized_question=context.question,
            approved_memories=approved,
            conversation_summary=conversation.summary or None,
        )

    @staticmethod
    def _memory_versions(pins: ContextVersionPins):
        from data_agent.memory import MemoryVersionPins

        return MemoryVersionPins(
            domain_version=pins.domain_version,
            binding_version=pins.binding_version,
            schema_fingerprint=pins.schema_fingerprint,
        )

    @staticmethod
    def _security_context(
        context: ExecutionContext,
        now: datetime,
    ) -> SecurityContext:
        role_text = ",".join(sorted(context.principal.roles)) or "none"
        return SecurityContext(
            principal=context.principal,
            rules=(
                ContextItem(
                    source=ContextSource.SECURITY,
                    key="security.tenant_scope",
                    content=f"tenant={context.principal.tenant_id};roles={role_text}",
                    version=context.bundle.runtime_version,
                    trust_level="verified",
                    sensitivity="restricted",
                    token_cost=1,
                    valid_from=now,
                ),
                ContextItem(
                    source=ContextSource.SECURITY,
                    key="security.read_only",
                    content="database access is read-only and policy governed",
                    version=context.bundle.runtime_version,
                    trust_level="verified",
                    sensitivity="internal",
                    token_cost=2,
                    valid_from=now,
                ),
            ),
        )

    @classmethod
    def _domain_items(
        cls,
        snapshot: BundleSnapshot,
        question: str,
        now: datetime,
    ) -> tuple[ContextItem, ...]:
        items: list[ContextItem] = []
        for metric_id, metric in snapshot.domain_pack.spec.metrics.items():
            content = f"{metric_id}: {metric.description}"
            items.append(
                ContextItem(
                    source=ContextSource.DOMAIN,
                    key=f"domain.metric.{metric_id}",
                    content=content,
                    version=snapshot.domain_pack.metadata.version,
                    trust_level="verified",
                    sensitivity="internal",
                    token_cost=cls._token_cost(content),
                    valid_from=now,
                    relevance=cls._relevance(question, content),
                )
            )
        for policy in snapshot.domain_pack.spec.policies:
            content = f"{policy.name}: {policy.description}"
            items.append(
                ContextItem(
                    source=ContextSource.DOMAIN,
                    key=f"domain.policy.{policy.name}",
                    content=content,
                    version=snapshot.domain_pack.metadata.version,
                    trust_level="verified",
                    sensitivity="internal",
                    token_cost=cls._token_cost(content),
                    valid_from=now,
                    relevance=1.0,
                )
            )
        return tuple(items)

    @classmethod
    def _binding_items(
        cls,
        snapshot: BundleSnapshot,
        now: datetime,
    ) -> tuple[ContextItem, ...]:
        policy = snapshot.enterprise_binding.spec.policies
        content = (
            f"access_mode={policy.access_mode};max_rows={policy.max_rows};"
            f"query_timeout_seconds={policy.query_timeout_seconds}"
        )
        return (
            ContextItem(
                source=ContextSource.BINDING,
                key="binding.access_constraints",
                content=content,
                version=snapshot.enterprise_binding.metadata.version,
                trust_level="verified",
                sensitivity="restricted",
                token_cost=cls._token_cost(content),
                valid_from=now,
                relevance=1.0,
            ),
        )

    @classmethod
    def _skill_items(
        cls,
        context: ExecutionContext,
        now: datetime,
    ) -> tuple[ContextItem, ...]:
        content = "allowed_tools=" + ",".join(context.allowed_tools)
        return (
            ContextItem(
                source=ContextSource.SKILL,
                key="skill.allowed_tools",
                content=content,
                version=context.skill_version,
                trust_level="verified",
                sensitivity="internal",
                token_cost=cls._token_cost(content),
                valid_from=now,
                relevance=1.0,
            ),
        )

    @classmethod
    def _memory_items(cls, records, question: str) -> tuple[ContextItem, ...]:
        items: list[ContextItem] = []
        for record in records:
            content = cls._memory_text(record.content)
            source = {
                MemoryScope.ENTERPRISE: ContextSource.APPROVED_ENTERPRISE_MEMORY,
                MemoryScope.EPISODIC: ContextSource.APPROVED_ENTERPRISE_MEMORY,
                MemoryScope.USER: ContextSource.USER_MEMORY,
                MemoryScope.CONVERSATION: ContextSource.CONVERSATION,
                MemoryScope.WORKING: ContextSource.EXECUTION_EVIDENCE,
            }[record.scope]
            owner_values = {
                name: getattr(record.owner, name)
                for name in (
                    "tenant_id",
                    "user_id",
                    "domain_id",
                    "conversation_id",
                    "run_id",
                )
                if hasattr(record.owner, name)
            }
            items.append(
                ContextItem(
                    source=source,
                    key=f"memory.{record.memory_id}",
                    content=content,
                    version=record.updated_at.isoformat(),
                    trust_level=record.trust_level.value,
                    sensitivity=record.sensitivity.value,
                    token_cost=cls._token_cost(content),
                    valid_from=record.created_at,
                    expires_at=record.expires_at,
                    owner=ContextOwner(**owner_values),
                    approved=True,
                    version_pins=ContextVersionPins(
                        domain_version=record.versions.domain_version,
                        binding_version=record.versions.binding_version,
                        schema_fingerprint=record.versions.schema_fingerprint,
                    ),
                    relevance=cls._relevance(question, content),
                )
            )
        return tuple(items)

    @staticmethod
    def _memory_text(content: Any) -> str:
        if isinstance(content, UserMemoryContent):
            return f"{content.preference_key}: {content.preference_value}"
        if isinstance(content, EnterpriseMemoryContent):
            return f"{content.category}: {content.statement}"
        if isinstance(content, EpisodicMemoryContent):
            return f"{content.event}: {content.lesson}; {content.outcome}"
        if isinstance(
            content,
            (WorkingMemoryContent, ConversationMemoryContent),
        ):
            return content.summary
        return json.dumps(content.model_dump(mode="json"), sort_keys=True)

    @staticmethod
    def _token_cost(value: str) -> int:
        return max(1, (len(value) + 3) // 4)

    @staticmethod
    def _relevance(question: str, content: str) -> float:
        query = {token.casefold() for token in question.split() if token.strip()}
        if not query:
            return 0.0
        text = content.casefold()
        matches = sum(token in text for token in query)
        return min(1.0, matches / len(query))


__all__ = ["RuntimeContextResolver"]
