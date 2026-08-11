from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.analysis_agent.models import DatasetAuthority
from data_agent.runtime.models import AgentMode, PrincipalContext
from data_agent.tools import (
    CredentialLease,
    ProviderContext,
    RetryPolicy,
    ToolBudget,
    ToolCall,
    ToolErrorCode,
    ToolError,
    ToolInvocationContext,
    ToolInvoker,
    ToolResult,
    ToolRegistry,
    ToolSpec,
)
from data_agent.relationships.router import GraphRouteError


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str


class _OtherPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int


class _Provider:
    def __init__(
        self,
        spec: ToolSpec,
        *,
        fail_times: int = 0,
        delay_seconds: float = 0.0,
    ) -> None:
        self.spec = spec
        self.fail_times = fail_times
        self.delay_seconds = delay_seconds
        self.calls = 0
        self.contexts: list[ProviderContext] = []

    async def invoke(
        self,
        payload: _Payload,
        context: ProviderContext,
    ) -> _Payload:
        self.calls += 1
        self.contexts.append(context)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.calls <= self.fail_times:
            raise ConnectionError("postgresql://user:secret@db/private")
        return _Payload(value=payload.value.upper())


class _Broker:
    def __init__(self) -> None:
        self.grants = []

    async def acquire(self, *, grant, source: str | None):
        self.grants.append(grant)
        now = datetime.now(UTC)
        return CredentialLease(
            credential_id="lease-1",
            grant_id=grant.grant_id,
            bundle_digest=grant.bundle_digest,
            source=source,
            connection_ref="secret://olist/local/database",
            capabilities=(grant.tool_name,),
            secret="postgresql://user:super-secret@db/private",
            issued_at=now,
            expires_at=grant.expires_at,
        )


class _SlowBroker:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    async def acquire(self, *, grant, source: str | None):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return None


class _RejectingBroker:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self, *, grant, source: str | None):
        self.calls += 1
        raise AssertionError("credential broker must not be called")


def _spec(**updates: object) -> ToolSpec:
    values = {
        "name": "query.compile",
        "version": "1.0.0",
        "description": "compile a governed logical plan",
        "input_schema": _Payload,
        "output_schema": _Payload,
        "risk_level": "medium",
        "side_effects": "read",
        "required_capabilities": ("query.compile",),
        "idempotency": "safe",
        "timeout_seconds": 1.0,
        "retry_policy": RetryPolicy(max_attempts=1),
        "examples": (),
        "eval_tags": ("contract",),
    }
    values.update(updates)
    return ToolSpec(**values)


def _context(*, allowed_tools=("query.compile",), max_calls: int = 3):
    return ToolInvocationContext(
        principal=PrincipalContext(
            tenant_id="seller-7",
            user_id="user-9",
            roles=("seller",),
        ),
        skill_id="commerce.analytics",
        skill_version="1.0.0",
        allowed_tools=allowed_tools,
        budget=ToolBudget(max_calls=max_calls),
        authority=DatasetAuthority(
            tenant_id="seller-7",
            user_id="user-9",
            source_id="sales",
            source_version=1,
            binding_id="sales-binding",
            binding_version=1,
            schema_fingerprint="sha256:" + "d" * 64,
            allowed_relation_ids=("public.orders",),
            mode=AgentMode.EXECUTE,
        ),
    )


class ToolRegistryTests(unittest.TestCase):
    def test_registry_rejects_manifest_provider_schema_drift(self) -> None:
        registry = ToolRegistry(version="1.0.0")
        spec = _spec()
        drifted = _Provider(_spec(output_schema=_OtherPayload))

        with self.assertRaisesRegex(ValueError, "provider manifest"):
            registry.register(spec, drifted)

    def test_allowed_view_is_closed_to_the_skill_allowlist(self) -> None:
        registry = ToolRegistry(version="1.0.0")
        first = _spec()
        second = _spec(
            name="semantic.search",
            required_capabilities=("semantic.search",),
        )
        registry.register(first, _Provider(first))
        registry.register(second, _Provider(second))
        registry.freeze()

        view = registry.allowed_view(_context(allowed_tools=("semantic.search",)))

        self.assertEqual(view.names(), ("semantic.search",))
        self.assertIsNone(view.get("query.compile"))
        with self.assertRaises(TypeError):
            registry.register(first, _Provider(first))

    def test_public_contracts_are_frozen_and_reject_extra_fields(self) -> None:
        call = ToolCall(
            call_id="call-1",
            tool_name="query.compile",
            tool_version="1.0.0",
            input_data=_Payload(value="x"),
        )

        with self.assertRaises(Exception):
            call.tool_name = "query.execute"  # type: ignore[misc]
        with self.assertRaises(Exception):
            ToolCall(
                call_id="call-2",
                tool_name="query.compile",
                tool_version="1.0.0",
                input_data=_Payload(value="x"),
                policy_decision_id="caller-forged",  # type: ignore[call-arg]
            )


class ToolInvokerTests(unittest.IsolatedAsyncioTestCase):
    def test_graph_route_errors_keep_their_actionable_codes(self) -> None:
        self.assertEqual(
            ToolInvoker._provider_error_classification(
                GraphRouteError(
                    "GRAPH_AMBIGUOUS_PATH",
                    "multiple equally safe relationship paths exist",
                )
            ),
            (ToolErrorCode.GRAPH_AMBIGUOUS_PATH, True),
        )

    async def test_invoker_validates_input_and_output_and_issues_short_grant(self) -> None:
        spec = _spec()
        provider = _Provider(spec)
        registry = ToolRegistry(version="1.0.0")
        registry.register(spec, provider)
        registry.freeze()
        broker = _Broker()
        invoker = ToolInvoker(registry, credential_broker=broker)
        call = ToolCall(
            call_id="call-1",
            tool_name=spec.name,
            tool_version=spec.version,
            input_data=_Payload(value="safe"),
        )

        result = await invoker.invoke(call, _context())

        self.assertEqual(result.status, "success")
        self.assertEqual(result.typed_data, _Payload(value="SAFE"))
        self.assertIsNone(result.structured_error)
        self.assertEqual(len(broker.grants), 1)
        grant = broker.grants[0]
        self.assertEqual(grant.principal_user_id, "user-9")
        self.assertEqual(grant.tenant_id, "seller-7")
        self.assertFalse(grant.admin_bypass)
        self.assertLessEqual(
            (grant.expires_at - datetime.now(UTC)).total_seconds(),
            5,
        )
        self.assertEqual(result.policy_decision_id, grant.policy_decision_id)
        self.assertEqual(provider.contexts[0].access_grant, grant)

    async def test_invoker_fails_closed_for_wrong_schema_and_disallowed_tool(self) -> None:
        spec = _spec()
        provider = _Provider(spec)
        registry = ToolRegistry(version="1.0.0")
        registry.register(spec, provider)
        registry.freeze()
        invoker = ToolInvoker(registry, credential_broker=_Broker())

        wrong = await invoker.invoke(
            ToolCall(
                call_id="bad-input",
                tool_name=spec.name,
                tool_version=spec.version,
                input_data=_OtherPayload(count=1),
            ),
            _context(),
        )
        denied = await invoker.invoke(
            ToolCall(
                call_id="denied",
                tool_name=spec.name,
                tool_version=spec.version,
                input_data=_Payload(value="x"),
            ),
            _context(allowed_tools=()),
        )

        self.assertEqual(wrong.structured_error.code, ToolErrorCode.INPUT_INVALID)
        self.assertEqual(denied.structured_error.code, ToolErrorCode.TOOL_NOT_ALLOWED)
        self.assertEqual(provider.calls, 0)

    async def test_budget_timeout_retry_and_redaction_have_stable_errors(self) -> None:
        retry_spec = _spec(
            retry_policy=RetryPolicy(max_attempts=2, initial_backoff_seconds=0),
        )
        retry_provider = _Provider(retry_spec, fail_times=1)
        timeout_spec = _spec(
            name="query.execute",
            required_capabilities=("query.execute",),
            timeout_seconds=0.01,
        )
        timeout_provider = _Provider(timeout_spec, delay_seconds=0.1)
        registry = ToolRegistry(version="1.0.0")
        registry.register(retry_spec, retry_provider)
        registry.register(timeout_spec, timeout_provider)
        registry.freeze()
        invoker = ToolInvoker(registry, credential_broker=_Broker())

        retried = await invoker.invoke(
            ToolCall(
                call_id="retry",
                tool_name=retry_spec.name,
                tool_version=retry_spec.version,
                input_data=_Payload(value="secret-input"),
                idempotency_key="stable-1",
            ),
            _context(allowed_tools=("query.compile", "query.execute")),
        )
        timed_out = await invoker.invoke(
            ToolCall(
                call_id="timeout",
                tool_name=timeout_spec.name,
                tool_version=timeout_spec.version,
                input_data=_Payload(value="secret-input"),
            ),
            _context(allowed_tools=("query.compile", "query.execute")),
        )
        budget_context = _context(max_calls=1)
        await invoker.invoke(
            ToolCall(
                call_id="first",
                tool_name=retry_spec.name,
                tool_version=retry_spec.version,
                input_data=_Payload(value="x"),
            ),
            budget_context,
        )
        exhausted = await invoker.invoke(
            ToolCall(
                call_id="second",
                tool_name=retry_spec.name,
                tool_version=retry_spec.version,
                input_data=_Payload(value="x"),
            ),
            budget_context,
        )

        self.assertEqual(retried.status, "success")
        self.assertEqual(retry_provider.calls, 3)
        self.assertEqual(timed_out.structured_error.code, ToolErrorCode.TIMEOUT)
        self.assertEqual(
            exhausted.structured_error.code,
            ToolErrorCode.BUDGET_EXCEEDED,
        )
        trace_json = json.dumps(
            retried.redacted_trace.model_dump(mode="json"),
            sort_keys=True,
        )
        self.assertNotIn("secret-input", trace_json)
        self.assertNotIn("super-secret", trace_json)
        self.assertNotIn("postgresql://", trace_json)

    async def test_broker_is_inside_deadline_and_offline_tools_get_no_credential(self) -> None:
        slow_spec = _spec(timeout_seconds=0.01)
        slow_provider = _Provider(slow_spec)
        slow_registry = ToolRegistry(version="1.0.0")
        slow_registry.register(slow_spec, slow_provider)
        slow_registry.freeze()
        slow_broker = _SlowBroker(0.1)

        timed_out = await ToolInvoker(
            slow_registry,
            credential_broker=slow_broker,
        ).invoke(
            ToolCall(
                call_id="broker-timeout",
                tool_name=slow_spec.name,
                input_data=_Payload(value="x"),
            ),
            _context(),
        )

        offline_spec = _spec(side_effects="none")
        offline_provider = _Provider(offline_spec)
        offline_registry = ToolRegistry(version="1.0.0")
        offline_registry.register(offline_spec, offline_provider)
        offline_registry.freeze()
        rejecting_broker = _RejectingBroker()
        offline = await ToolInvoker(
            offline_registry,
            credential_broker=rejecting_broker,
        ).invoke(
            ToolCall(
                call_id="offline",
                tool_name=offline_spec.name,
                input_data=_Payload(value="x"),
            ),
            _context(),
        )

        self.assertEqual(timed_out.structured_error.code, ToolErrorCode.TIMEOUT)
        self.assertEqual(slow_provider.calls, 0)
        self.assertEqual(offline.status, "success")
        self.assertEqual(rejecting_broker.calls, 0)
        self.assertIsNone(offline_provider.contexts[0].credential)

    async def test_required_idempotency_key_and_result_state_are_enforced(self) -> None:
        spec = _spec(idempotency="required")
        provider = _Provider(spec)
        registry = ToolRegistry(version="1.0.0")
        registry.register(spec, provider)
        registry.freeze()
        result = await ToolInvoker(registry, credential_broker=_Broker()).invoke(
            ToolCall(
                call_id="missing-key",
                tool_name=spec.name,
                input_data=_Payload(value="x"),
            ),
            _context(),
        )

        self.assertEqual(
            result.structured_error.code,
            ToolErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        )
        self.assertEqual(provider.calls, 0)
        with self.assertRaises(Exception):
            ToolResult(
                status="success",
                typed_data=_Payload(value="x"),
                structured_error=ToolError(
                    code=ToolErrorCode.PROVIDER_ERROR,
                    message="bad state",
                ),
                redacted_trace=result.redacted_trace,
            )


if __name__ == "__main__":
    unittest.main()
