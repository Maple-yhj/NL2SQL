"""Pure, deterministic assembly of governed runtime context."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from .models import ContractModel, PrincipalContext


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ContextSource(StrEnum):
    SECURITY = "security"
    DOMAIN = "domain"
    BINDING = "binding"
    SKILL = "skill"
    APPROVED_ENTERPRISE_MEMORY = "approved_enterprise_memory"
    USER_MEMORY = "user_memory"
    CONVERSATION = "conversation"
    EXECUTION_EVIDENCE = "execution_evidence"


CONTEXT_PRECEDENCE: tuple[ContextSource, ...] = tuple(ContextSource)
_PRIORITY = {source: index for index, source in enumerate(CONTEXT_PRECEDENCE)}
_MEMORY_SOURCES = {
    ContextSource.APPROVED_ENTERPRISE_MEMORY,
    ContextSource.USER_MEMORY,
    ContextSource.CONVERSATION,
}
_MANDATORY_SOURCES = {
    ContextSource.SECURITY,
    ContextSource.DOMAIN,
    ContextSource.BINDING,
    ContextSource.SKILL,
}


class ContextBudgetExceededError(ValueError):
    """Raised when governed hard context cannot fit without weakening it."""

    code = "CONTEXT_BUDGET_EXCEEDED"

    def __init__(self) -> None:
        super().__init__("mandatory context exceeds the governed context budget")


class ContextVersionPins(ContractModel):
    domain_version: NonBlankText | None = None
    binding_version: NonBlankText | None = None
    skill_version: NonBlankText | None = None
    schema_fingerprint: NonBlankText | None = None


class ContextOwner(ContractModel):
    tenant_id: NonBlankText
    user_id: NonBlankText | None = None
    domain_id: NonBlankText | None = None
    conversation_id: NonBlankText | None = None
    run_id: NonBlankText | None = None


class ContextItem(ContractModel):
    """One prompt-safe context fact plus its authority metadata."""

    source: ContextSource
    key: NonBlankText
    content: NonBlankText
    version: NonBlankText
    trust_level: Annotated[str, StringConstraints(pattern=r"^(low|medium|high|verified)$")]
    sensitivity: Annotated[
        str,
        StringConstraints(pattern=r"^(public|internal|restricted)$"),
    ]
    token_cost: int = Field(ge=1)
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    owner: ContextOwner | None = None
    approved: bool = False
    version_pins: ContextVersionPins = Field(default_factory=ContextVersionPins)
    relevance: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_validity_window(self) -> "ContextItem":
        for name in ("valid_from", "expires_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if (
            self.valid_from is not None
            and self.expires_at is not None
            and self.expires_at <= self.valid_from
        ):
            raise ValueError("context validity window must be positive")
        return self


class SecurityContext(ContractModel):
    principal: PrincipalContext
    rules: tuple[ContextItem, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_security_rules(self) -> "SecurityContext":
        if any(item.source != ContextSource.SECURITY for item in self.rules):
            raise ValueError("SecurityContext accepts only security items")
        keys = tuple(item.key for item in self.rules)
        if len(keys) != len(set(keys)):
            raise ValueError("security context keys must be unique")
        return self


class ContextBudget(ContractModel):
    """Total envelope budget; every hard-authority item must fit."""

    max_items: int = Field(default=64, ge=0)
    max_tokens: int = Field(default=4096, ge=0)


class ContextEnvelope(ContractModel):
    principal_context: SecurityContext
    domain_semantic_context: tuple[ContextItem, ...] = ()
    enterprise_binding_context: tuple[ContextItem, ...] = ()
    skill_context: tuple[ContextItem, ...] = ()
    approved_enterprise_memory_context: tuple[ContextItem, ...] = ()
    user_memory_context: tuple[ContextItem, ...] = ()
    conversation_context: tuple[ContextItem, ...] = ()
    execution_evidence: tuple[ContextItem, ...] = ()
    used_tokens: int = Field(default=0, ge=0)
    truncated: bool = False

    @property
    def approved_memory_context(self) -> tuple[ContextItem, ...]:
        return (
            *self.approved_enterprise_memory_context,
            *self.user_memory_context,
        )

    @property
    def items(self) -> tuple[ContextItem, ...]:
        return (
            *self.principal_context.rules,
            *self.domain_semantic_context,
            *self.enterprise_binding_context,
            *self.skill_context,
            *self.approved_enterprise_memory_context,
            *self.user_memory_context,
            *self.conversation_context,
            *self.execution_evidence,
        )


class ContextAssembler:
    """Stateless context selection; this class performs no I/O."""

    __slots__ = ()

    def assemble(
        self,
        *,
        security_context: SecurityContext,
        items: tuple[ContextItem, ...],
        pins: ContextVersionPins,
        budget: ContextBudget,
        now: datetime,
        domain_id: str,
        conversation_id: str | None,
        run_id: str,
    ) -> ContextEnvelope:
        if now.tzinfo is None:
            raise ValueError("context assembly time must be timezone-aware")

        eligible = tuple(
            item
            for item in (*security_context.rules, *items)
            if self._eligible(
                item,
                security_context=security_context,
                pins=pins,
                now=now,
                domain_id=domain_id,
                conversation_id=conversation_id,
                run_id=run_id,
            )
        )
        ordered = sorted(
            eligible,
            key=lambda item: (
                _PRIORITY[item.source],
                -item.relevance,
                item.key,
                item.version,
                item.content,
            ),
        )

        # Reserve each key for the highest valid authority before applying any
        # budget.  A skipped high-priority item therefore cannot be replaced by
        # a smaller lower-priority item with the same key.
        claims: dict[str, ContextItem] = {}
        for item in ordered:
            claims.setdefault(item.key, item)
        authoritative = tuple(claims.values())
        mandatory = tuple(
            item for item in authoritative if item.source in _MANDATORY_SOURCES
        )
        mandatory_tokens = sum(item.token_cost for item in mandatory)
        if (
            len(mandatory) > budget.max_items
            or mandatory_tokens > budget.max_tokens
        ):
            raise ContextBudgetExceededError()

        selected: list[ContextItem] = list(mandatory)
        used_tokens = mandatory_tokens
        truncated = False
        for item in authoritative:
            if item.source in _MANDATORY_SOURCES:
                continue
            if (
                len(selected) >= budget.max_items
                or used_tokens + item.token_cost > budget.max_tokens
            ):
                truncated = True
                continue
            selected.append(item)
            used_tokens += item.token_cost

        grouped = {
            source: tuple(item for item in selected if item.source == source)
            for source in ContextSource
        }
        return ContextEnvelope(
            principal_context=security_context,
            domain_semantic_context=grouped[ContextSource.DOMAIN],
            enterprise_binding_context=grouped[ContextSource.BINDING],
            skill_context=grouped[ContextSource.SKILL],
            approved_enterprise_memory_context=grouped[
                ContextSource.APPROVED_ENTERPRISE_MEMORY
            ],
            user_memory_context=grouped[ContextSource.USER_MEMORY],
            conversation_context=grouped[ContextSource.CONVERSATION],
            execution_evidence=grouped[ContextSource.EXECUTION_EVIDENCE],
            used_tokens=used_tokens,
            truncated=truncated,
        )

    @staticmethod
    def _eligible(
        item: ContextItem,
        *,
        security_context: SecurityContext,
        pins: ContextVersionPins,
        now: datetime,
        domain_id: str,
        conversation_id: str | None,
        run_id: str,
    ) -> bool:
        if item.valid_from is not None and item.valid_from > now:
            return False
        if item.expires_at is not None and item.expires_at <= now:
            return False
        if item.source in _MEMORY_SOURCES and not item.approved:
            return False
        if not ContextAssembler._versions_match(item, pins):
            return False
        return ContextAssembler._owner_matches(
            item,
            principal=security_context.principal,
            domain_id=domain_id,
            conversation_id=conversation_id,
            run_id=run_id,
        )

    @staticmethod
    def _versions_match(item: ContextItem, pins: ContextVersionPins) -> bool:
        direct_pin = {
            ContextSource.DOMAIN: pins.domain_version,
            ContextSource.BINDING: pins.binding_version,
            ContextSource.SKILL: pins.skill_version,
        }.get(item.source)
        if direct_pin is not None and item.version != direct_pin:
            return False
        for field_name in type(pins).model_fields:
            required = getattr(item.version_pins, field_name)
            if required is not None and required != getattr(pins, field_name):
                return False
        return True

    @staticmethod
    def _owner_matches(
        item: ContextItem,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str | None,
        run_id: str,
    ) -> bool:
        source = item.source
        owner = item.owner
        if source in {
            ContextSource.APPROVED_ENTERPRISE_MEMORY,
            ContextSource.USER_MEMORY,
            ContextSource.CONVERSATION,
            ContextSource.EXECUTION_EVIDENCE,
        } and owner is None:
            return False
        if owner is None:
            return True
        expected = {
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "domain_id": domain_id,
            "conversation_id": conversation_id,
            "run_id": run_id,
        }
        if any(
            getattr(owner, name) is not None
            and getattr(owner, name) != value
            for name, value in expected.items()
        ):
            return False
        required_fields = {
            ContextSource.APPROVED_ENTERPRISE_MEMORY: ("tenant_id", "domain_id"),
            ContextSource.USER_MEMORY: ("tenant_id", "user_id"),
            ContextSource.CONVERSATION: (
                "tenant_id",
                "user_id",
                "domain_id",
                "conversation_id",
            ),
            ContextSource.EXECUTION_EVIDENCE: (
                "tenant_id",
                "user_id",
                "domain_id",
                "conversation_id",
                "run_id",
            ),
        }.get(source, ())
        return all(getattr(owner, name) is not None for name in required_fields)


__all__ = [
    "CONTEXT_PRECEDENCE",
    "ContextAssembler",
    "ContextBudget",
    "ContextBudgetExceededError",
    "ContextEnvelope",
    "ContextItem",
    "ContextOwner",
    "ContextSource",
    "ContextVersionPins",
    "SecurityContext",
]
