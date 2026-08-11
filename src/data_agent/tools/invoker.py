"""Unified asynchronous invocation pipeline for every governed tool."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from pydantic import BaseModel, ValidationError

from data_agent.analysis_agent.models import stable_digest

from .models import (
    AccessGrant,
    ArtifactRef,
    CredentialBroker,
    NullCredentialBroker,
    ProviderContext,
    ToolCall,
    ToolError,
    ToolErrorCode,
    ToolInvocationContext,
    ToolLineage,
    ToolResult,
    ToolSpec,
    ToolTrace,
)
from .registry import ToolRegistry


AuditSink = Callable[[ToolTrace], None | Awaitable[None]]
logger = logging.getLogger(__name__)


def stable_grant_id(decision: str, tool_name: str, issued_at: datetime) -> str:
    return stable_digest(
        {
            "policy_decision_id": decision,
            "tool_name": tool_name,
            "issued_at": issued_at.isoformat(),
        }
    )


def _logical_plan_hash(payload: BaseModel) -> str | None:
    plan = getattr(payload, "logical_plan", None) or getattr(payload, "plan", None)
    if plan is not None:
        stable_hash = getattr(plan, "stable_hash", None)
        if callable(stable_hash):
            return str(stable_hash())
    prepared = getattr(payload, "prepared_query", None)
    value = getattr(prepared, "logical_plan_hash", None)
    if value is None:
        evidence = getattr(payload, "data", None)
        value = getattr(evidence, "logical_plan_hash", None)
    return str(value) if value else None


def policy_decision_id(
    context: ToolInvocationContext,
    logical_plan_hash: str | None,
) -> str:
    return stable_digest(
        {
            "authority": context.authority.model_dump(mode="json"),
            "logical_plan_hash": logical_plan_hash,
        }
    )


def _issue_access_grant(
    spec: ToolSpec,
    payload: BaseModel,
    context: ToolInvocationContext,
) -> AccessGrant:
    authority = context.authority
    admin_bypass = False
    allowed_relations = authority.allowed_relation_ids
    max_rows = context.max_rows
    policy_timeout_seconds = context.statement_timeout_ms / 1000
    bundle_digest = stable_digest(authority.model_dump(mode="json"))
    source = authority.source_id if spec.credential_requirement == "required" else None
    timeout_seconds = min(spec.timeout_seconds, policy_timeout_seconds)
    logical_hash = _logical_plan_hash(payload)
    decision = policy_decision_id(context, logical_hash)
    now = datetime.now(UTC)
    prepared = getattr(payload, "prepared_query", None)
    prepared_hash = getattr(prepared, "sql_ast_hash", None)
    if prepared_hash is None:
        evidence = getattr(payload, "data", None)
        prepared_hash = getattr(evidence, "query_hash", None)
    return AccessGrant(
        grant_id="grant_" + stable_grant_id(decision, spec.name, now),
        tool_name=spec.name,
        tool_version=spec.version,
        skill_id=context.skill_id,
        bundle_digest=bundle_digest,
        schema_fingerprint=authority.schema_fingerprint,
        source=source,
        read_only=True,
        principal_user_id=context.principal.user_id,
        tenant_id=context.principal.tenant_id,
        admin_bypass=admin_bypass,
        allowed_relations=tuple(allowed_relations),
        max_rows=min(max_rows, context.max_rows),
        statement_timeout_ms=max(1, int(timeout_seconds * 1000)),
        policy_decision_id=decision,
        logical_plan_hash=logical_hash,
        prepared_query_hash=str(prepared_hash) if prepared_hash else None,
        issued_at=now,
        expires_at=now + timedelta(seconds=timeout_seconds),
    )


class ToolInvoker:
    """Execute the fixed validation, authorization, provider and audit chain."""

    __slots__ = ("_registry", "_credential_broker", "_audit_sink")

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        credential_broker: CredentialBroker | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        if not registry.frozen:
            raise ValueError("ToolInvoker requires a frozen ToolRegistry")
        self._registry = registry
        self._credential_broker = credential_broker or NullCredentialBroker()
        self._audit_sink = audit_sink

    async def invoke(
        self,
        call: ToolCall,
        context: ToolInvocationContext,
    ) -> ToolResult:
        started_at = datetime.now(UTC)
        started_clock = monotonic()
        spec = self._registry.get(call.tool_name)
        if spec is None:
            return await self._error_result(
                call,
                None,
                started_at,
                started_clock,
                ToolErrorCode.TOOL_NOT_FOUND,
                "requested tool is not registered",
            )
        if call.tool_version != spec.version:
            return await self._error_result(
                call,
                spec,
                started_at,
                started_clock,
                ToolErrorCode.VERSION_MISMATCH,
                "requested tool version is not available",
            )
        try:
            validated_input = spec.input_schema.model_validate(
                call.input_data.model_dump(mode="python", warnings=False)
            )
        except (AttributeError, TypeError, ValueError, ValidationError):
            return await self._error_result(
                call,
                spec,
                started_at,
                started_clock,
                ToolErrorCode.INPUT_INVALID,
                "tool input does not satisfy its declared schema",
            )
        if _contains_forbidden_authority_fields(
            validated_input.model_dump(mode="python", warnings=False)
        ):
            return await self._error_result(
                call,
                spec,
                started_at,
                started_clock,
                ToolErrorCode.INPUT_INVALID,
                "tool input cannot override runtime authority",
            )

        if self._registry.allowed_view(context).get(call.tool_name) is None:
            return await self._error_result(
                call,
                spec,
                started_at,
                started_clock,
                ToolErrorCode.TOOL_NOT_ALLOWED,
                "tool is not allowed for the selected skill",
            )
        if spec.idempotency == "required" and call.idempotency_key is None:
            return await self._error_result(
                call,
                spec,
                started_at,
                started_clock,
                ToolErrorCode.IDEMPOTENCY_KEY_REQUIRED,
                "tool requires an idempotency key",
            )
        if not await context.budget.consume():
            return await self._error_result(
                call,
                spec,
                started_at,
                started_clock,
                ToolErrorCode.BUDGET_EXCEEDED,
                "tool call budget is exhausted",
            )

        grant = _issue_access_grant(spec, validated_input, context)
        provider = self._registry.provider(call.tool_name)
        if provider is None:
            return await self._error_result(
                call,
                spec,
                started_at,
                started_clock,
                ToolErrorCode.TOOL_NOT_FOUND,
                "requested tool provider is unavailable",
                policy_decision_id=grant.policy_decision_id,
            )

        attempts = 0
        stage = "credential"
        try:
            async with asyncio.timeout(spec.timeout_seconds):
                credential = None
                if spec.credential_requirement == "required":
                    credential = await self._credential_broker.acquire(
                        grant=grant,
                        source=grant.source,
                    )
                    if credential is None or (
                        credential.expires_at <= datetime.now(UTC)
                        or credential.grant_id != grant.grant_id
                        or credential.bundle_digest != grant.bundle_digest
                        or credential.source != grant.source
                        or spec.name not in credential.capabilities
                    ):
                        raise PermissionError("credential lease authority is invalid")
                provider_context = ProviderContext(
                    call_id=call.call_id,
                    run_id=context.run_id,
                    principal=context.principal,
                    authority=context.authority,
                    runtime_resources=context.runtime_resources,
                    access_grant=grant,
                    credential=credential,
                )
                stage = "provider"
                while True:
                    attempts += 1
                    try:
                        output = await provider.invoke(validated_input, provider_context)
                        break
                    except asyncio.CancelledError:
                        raise
                    except (ConnectionError, OSError):
                        can_retry = (
                            spec.idempotency != "none"
                            and attempts < spec.retry_policy.max_attempts
                        )
                        if not can_retry:
                            raise
                        delay = min(
                            spec.retry_policy.initial_backoff_seconds
                            * (2 ** (attempts - 1)),
                            spec.retry_policy.max_backoff_seconds,
                        )
                        if delay:
                            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return await self._error_result(
                call,
                spec,
                started_at,
                started_clock,
                ToolErrorCode.TIMEOUT,
                "tool invocation exceeded its deadline",
                attempts=attempts,
                policy_decision_id=grant.policy_decision_id,
            )
        except Exception as exc:
            if stage == "credential":
                code = ToolErrorCode.CREDENTIAL_UNAVAILABLE
                retryable = False
            else:
                code, retryable = self._provider_error_classification(exc)
            diagnostic_id = stable_digest(
                {
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "stage": stage,
                    "error_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                    "provider_code": str(getattr(exc, "code", "")),
                    "message": str(exc)[:2000],
                }
            )[:16]
            logger.error(
                "tool invocation failed call_id=%s tool_name=%s stage=%s "
                "error_type=%s provider_code=%s diagnostic_id=%s",
                call.call_id,
                call.tool_name,
                stage,
                f"{type(exc).__module__}.{type(exc).__qualname__}",
                str(getattr(exc, "code", "unavailable"))[:80],
                diagnostic_id,
            )
            return await self._error_result(
                call,
                spec,
                started_at,
                started_clock,
                code,
                (
                    "credential broker could not issue a lease "
                    f"(diagnostic_id={diagnostic_id})"
                    if stage == "credential"
                    else f"tool provider failed (diagnostic_id={diagnostic_id})"
                ),
                attempts=attempts,
                policy_decision_id=grant.policy_decision_id,
                retryable=retryable,
            )

        try:
            validated_output = spec.output_schema.model_validate(
                output.model_dump(mode="python", warnings=False)
            )
        except (AttributeError, TypeError, ValueError, ValidationError):
            return await self._error_result(
                call,
                spec,
                started_at,
                started_clock,
                ToolErrorCode.OUTPUT_INVALID,
                "tool output does not satisfy its declared schema",
                attempts=attempts,
                policy_decision_id=grant.policy_decision_id,
            )

        artifact = getattr(validated_output, "artifact", None)
        evidence = getattr(validated_output, "evidence", None)
        rows = int(
            getattr(validated_output, "row_count", 0)
            or getattr(artifact, "row_count", 0)
            or 0
        )
        artifact_refs = (
            (ArtifactRef(artifact_id=artifact.artifact_id, media_type="application/json"),)
            if artifact is not None
            else ()
        )
        evidence_ids = (
            (evidence.evidence_id,)
            if evidence is not None
            else ()
        )
        finished_at = datetime.now(UTC)
        latency_ms = max(0, int((monotonic() - started_clock) * 1000))
        trace = ToolTrace(
            call_id=call.call_id,
            tool_name=call.tool_name,
            tool_version=call.tool_version,
            status="success",
            attempts=attempts,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=latency_ms,
            input_schema=spec.input_schema.__name__,
            output_schema=spec.output_schema.__name__,
            safe_args_digest=_safe_args_digest(call),
            artifact_ids=tuple(item.artifact_id for item in artifact_refs),
            evidence_ids=evidence_ids,
        )
        await self._audit(trace)
        return ToolResult(
            status="success",
            typed_data=validated_output,
            artifact_refs=artifact_refs,
            rows=rows,
            latency_ms=latency_ms,
            lineage=ToolLineage(
                logical_plan_hash=grant.logical_plan_hash,
                query_hash=grant.prepared_query_hash,
                evidence_ids=evidence_ids,
            ),
            policy_decision_id=grant.policy_decision_id,
            redacted_trace=trace,
        )

    async def _error_result(
        self,
        call: ToolCall,
        spec: ToolSpec | None,
        started_at: datetime,
        started_clock: float,
        code: ToolErrorCode,
        message: str,
        *,
        attempts: int = 0,
        policy_decision_id: str | None = None,
        retryable: bool = False,
    ) -> ToolResult:
        finished_at = datetime.now(UTC)
        latency_ms = max(0, int((monotonic() - started_clock) * 1000))
        input_name = spec.input_schema.__name__ if spec else "UnavailableInput"
        output_name = spec.output_schema.__name__ if spec else "UnavailableOutput"
        trace = ToolTrace(
            call_id=call.call_id,
            tool_name=call.tool_name,
            tool_version=call.tool_version,
            status="error",
            attempts=attempts,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=latency_ms,
            input_schema=input_name,
            output_schema=output_name,
            safe_args_digest=_safe_args_digest(call),
            error_code=code,
        )
        await self._audit(trace)
        return ToolResult(
            status="error",
            structured_error=ToolError(
                code=code,
                message=message,
                retryable=retryable,
            ),
            latency_ms=latency_ms,
            policy_decision_id=policy_decision_id,
            redacted_trace=trace,
        )

    async def _audit(self, trace: ToolTrace) -> None:
        if self._audit_sink is None:
            return
        result: Any = self._audit_sink(trace)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _provider_error_code(exc: Exception) -> ToolErrorCode:
        return ToolInvoker._provider_error_classification(exc)[0]

    @staticmethod
    def _provider_error_classification(
        exc: Exception,
    ) -> tuple[ToolErrorCode, bool]:
        exception_code = getattr(exc, "code", None)
        raw_code = getattr(exception_code, "value", exception_code)
        mapping = {
            "GRANT_EXPIRED": (ToolErrorCode.GRANT_EXPIRED, False),
            "GRANT_MISMATCH": (ToolErrorCode.GRANT_INVALID, False),
            "RELATION_NOT_ALLOWED": (ToolErrorCode.RELATION_NOT_ALLOWED, False),
            "ROW_LIMIT_EXCEEDED": (ToolErrorCode.ROW_LIMIT_EXCEEDED, True),
            "TIMEOUT": (ToolErrorCode.TIMEOUT, False),
            "DATABASE_UNAVAILABLE": (ToolErrorCode.CONNECTOR_UNAVAILABLE, False),
            "BUNDLE_MISMATCH": (ToolErrorCode.BINDING_STALE, True),
            "UNKNOWN_RELATION": (ToolErrorCode.BINDING_STALE, True),
            "SQL_COMPILE_ERROR": (ToolErrorCode.SQL_COMPILE_ERROR, True),
            "LOGICAL_PLAN_INVALID": (ToolErrorCode.LOGICAL_PLAN_INVALID, True),
            "GRAPH_NO_PATH": (ToolErrorCode.GRAPH_NO_PATH, False),
            "GRAPH_AMBIGUOUS_PATH": (ToolErrorCode.GRAPH_AMBIGUOUS_PATH, True),
            "GRAPH_UNSAFE_FANOUT": (ToolErrorCode.GRAPH_UNSAFE_FANOUT, False),
            "UNKNOWN_REFERENCE": (ToolErrorCode.LOGICAL_PLAN_INVALID, True),
            "DISCONNECTED_PLAN": (ToolErrorCode.LOGICAL_PLAN_INVALID, True),
            "POLICY_INVALID": (ToolErrorCode.POLICY_VIOLATION, False),
            "BOUND_PLAN_MISMATCH": (ToolErrorCode.POLICY_VIOLATION, False),
            "POLICY_VIOLATION": (ToolErrorCode.POLICY_VIOLATION, False),
            "ACCESS_DENIED": (ToolErrorCode.ACCESS_DENIED, False),
            "AGENT_ARTIFACT_NOT_FOUND": (ToolErrorCode.ACCESS_DENIED, False),
            "AGENT_ARTIFACT_INTEGRITY_ERROR": (ToolErrorCode.ACCESS_DENIED, False),
        }
        return mapping.get(raw_code, (ToolErrorCode.PROVIDER_ERROR, False))


_FORBIDDEN_AUTHORITY_FIELDS = frozenset(
    {
        "authority",
        "tenant_id",
        "user_id",
        "source_id",
        "source_version",
        "binding_id",
        "binding_version",
        "allowed_relation_ids",
        "credential",
        "credential_ref",
        "secret",
        "dsn",
        "raw_sql",
        "code",
        "file_path",
        "path",
    }
)


def _contains_forbidden_authority_fields(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_AUTHORITY_FIELDS:
                return True
            if _contains_forbidden_authority_fields(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_authority_fields(item) for item in value)
    return False


def _safe_args_digest(call: ToolCall) -> str | None:
    try:
        return stable_digest(
            call.input_data.model_dump(mode="json", warnings=False)
        )
    except (AttributeError, TypeError, ValueError):
        return None
