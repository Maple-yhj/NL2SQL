"""Composition roots for the upload-only product and opt-in pack runtimes."""

from __future__ import annotations

import inspect
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from data_agent.execution import (
    COMMERCE_EXECUTION_GRAPH,
    ExecutionDependencies,
    InternalGraphExecutor,
)
from data_agent.memory import PostgresMemoryManager
from data_agent.tools import CredentialLease, ToolInvoker
from data_agent.tools.connectors.postgres import PostgresConnector
from data_agent.tools.providers import build_builtin_registry

from .bundle_store import BundlePaths, BundleSnapshot, BundleStore
from .composition import stable_digest
from .context import ContextAssembler
from .context_resolver import RuntimeContextResolver
from .dependencies import ModelClient, RuntimeDependencies
from .planner import ModelLogicalPlanner
from .paths import resolve_bundle_paths, validate_bundle_paths
from .service import DefaultDataAgentRuntime
from .upload_runtime import UploadDatasetRuntime, UploadRuntimeComposition


PoolFactory = Callable[..., Awaitable[Any]]
ModelClientFactory = Callable[[], ModelClient | Awaitable[ModelClient]]


async def _asyncpg_pool_factory(
    *,
    dsn: str,
    min_size: int,
    max_size: int,
) -> Any:
    import asyncpg

    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
    )


class _ConfiguredModelClient:
    def __init__(self, client: Any, *, model_id: str, version: str) -> None:
        self._client = client
        self.model_id = model_id
        self.version = version

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str:
        return await self._client.complete(
            prompt=prompt,
            system=system,
            max_output_tokens=max_output_tokens,
        )


def _default_model_client_factory(environment: Mapping[str, str]) -> ModelClient:
    from data_agent.adapters.llm import (
        create_llm_from_config,
        resolve_llm_config,
    )

    config = resolve_llm_config(environment)
    return _ConfiguredModelClient(
        create_llm_from_config(config),
        model_id=f"{config.provider}.planner",
        version=config.model_name,
    )


class EnvironmentCredentialBroker:
    """Issue short-lived leases from deployment secret refs after startup."""

    def __init__(
        self,
        *,
        source_refs: Mapping[str, str],
        secret_values: Mapping[str, str],
    ) -> None:
        self._source_refs = dict(source_refs)
        self._secret_values = dict(secret_values)

    async def acquire(self, *, grant, source: str | None):
        if source is None:
            return None
        connection_ref = self._source_refs.get(source)
        secret = self._secret_values.get(connection_ref or "")
        if connection_ref is None or not secret:
            return None
        now = datetime.now(UTC)
        return CredentialLease(
            credential_id="lease:" + stable_digest(
                {
                    "grant_id": grant.grant_id,
                    "source": source,
                    "issued_at": now.isoformat(),
                }
            )[:24],
            grant_id=grant.grant_id,
            bundle_digest=grant.bundle_digest,
            source=source,
            connection_ref=connection_ref,
            capabilities=(grant.tool_name,),
            secret=secret,
            issued_at=now,
            expires_at=grant.expires_at,
        )


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    runtime: DefaultDataAgentRuntime
    dependencies: RuntimeDependencies
    snapshot: BundleSnapshot

    async def close(self) -> None:
        await self.runtime.close()


async def build_upload_runtime(
    *,
    state_root: str | Path | None = None,
    model_client_factory: ModelClientFactory | None = None,
    environment: Mapping[str, str] | None = None,
) -> UploadRuntimeComposition:
    """Build the default product without activating any bundled datasource."""

    env = dict(os.environ if environment is None else environment)
    configured_root = state_root or env.get("DATA_AGENT_STATE_DIR")
    control_root = Path(configured_root or "var/data-agent").expanduser()
    factory = model_client_factory or (
        lambda: _default_model_client_factory(env)
    )
    model_client = factory()
    if inspect.isawaitable(model_client):
        model_client = await model_client
    if not isinstance(model_client.model_id, str) or not isinstance(
        model_client.version, str
    ):
        await _close_resource(model_client)
        raise TypeError("model client must expose stable id and version pins")
    runtime = UploadDatasetRuntime(
        control_root / "control" / "conversations.sqlite3"
    )
    return UploadRuntimeComposition(
        runtime=runtime,
        model_client=model_client,
    )


async def build_runtime(
    *,
    bundle: BundlePaths | None = None,
    project_root: str | Path | None = None,
    pool_factory: PoolFactory | None = None,
    model_client_factory: ModelClientFactory | None = None,
    environment: Mapping[str, str] | None = None,
) -> RuntimeComposition:
    """Load verified packs first, then create one pool and one runtime graph."""

    if bundle is not None and project_root is not None:
        raise ValueError("bundle and project_root are mutually exclusive")
    selected_paths = (
        validate_bundle_paths(bundle)
        if bundle is not None
        else resolve_bundle_paths(project_root, environment=environment)
    )
    store = BundleStore()
    snapshot = store.load_and_activate(selected_paths)
    env = dict(os.environ if environment is None else environment)
    sources = snapshot.enterprise_binding.spec.sources
    if len(sources) != 1:
        raise ValueError("runtime requires exactly one governed datasource")
    source_name, source = next(iter(sources.items()))
    secret_name = snapshot.deployment_profile.spec.datasource_secrets.get(
        source.connection_ref
    )
    secret_value = env.get(secret_name or "")
    if not secret_name or not secret_value:
        raise ValueError(
            f"deployment secret {secret_name or source.connection_ref} is unavailable"
        )

    create_pool = pool_factory or _asyncpg_pool_factory
    pool = await create_pool(dsn=secret_value, min_size=1, max_size=10)
    try:
        factory = model_client_factory or (
            lambda: _default_model_client_factory(env)
        )
        model_client = factory()
        if inspect.isawaitable(model_client):
            model_client = await model_client
        if not isinstance(model_client.model_id, str) or not isinstance(
            model_client.version, str
        ):
            raise TypeError("model client must expose stable id and version pins")

        memory = PostgresMemoryManager(pool)
        connector = PostgresConnector(
            pool,
            allowed_relations=tuple(
                snapshot.bundle.compiled_access_policy.get("relationAllowlist", ())
            ),
            schema_fingerprint=snapshot.bundle.schema_fingerprint,
            source=source_name,
            connection_ref=source.connection_ref,
            bundle_digest=None,
        )
        registry = build_builtin_registry(
            snapshot.domain_pack,
            snapshot.enterprise_binding,
            snapshot.bundle,
            connector,
        )
        broker = EnvironmentCredentialBroker(
            source_refs={source_name: source.connection_ref},
            secret_values={source.connection_ref: secret_value},
        )
        invoker = ToolInvoker(registry, credential_broker=broker)
        resolver = RuntimeContextResolver(
            memory=memory,
            assembler=ContextAssembler(),
        )
        planner = ModelLogicalPlanner(model_client)
        executor = InternalGraphExecutor(
            COMMERCE_EXECUTION_GRAPH,
            ExecutionDependencies(
                invoker=invoker,
                context_resolver=resolver,
                planner=planner,
                domain_pack=snapshot.domain_pack,
            ),
        )
        resources: list[Any] = [pool]
        if callable(getattr(model_client, "close", None)):
            resources.append(model_client)
        dependencies = RuntimeDependencies(
            bundle_store=store,
            skill_registry=__import__(
                "data_agent.skills", fromlist=["BUILTIN_SKILL_REGISTRY"]
            ).BUILTIN_SKILL_REGISTRY,
            tool_registry=registry,
            graph=COMMERCE_EXECUTION_GRAPH,
            executor=executor,
            memory=memory,
            context_resolver=resolver,
            planner=planner,
            model_client=model_client,
            resources=tuple(resources),
        )
        runtime = DefaultDataAgentRuntime(dependencies)
        return RuntimeComposition(
            runtime=runtime,
            dependencies=dependencies,
            snapshot=store.snapshot(),
        )
    except BaseException:
        await _close_resource(pool)
        raise


async def build_olist_runtime(
    *,
    project_root: str | Path | None = None,
    pool_factory: PoolFactory | None = None,
    model_client_factory: ModelClientFactory | None = None,
    environment: Mapping[str, str] | None = None,
) -> RuntimeComposition:
    """Backwards-compatible alias for the bundled OList deployment."""

    return await build_runtime(
        project_root=project_root,
        pool_factory=pool_factory,
        model_client_factory=model_client_factory,
        environment=environment,
    )


async def _close_resource(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if close is None:
        return
    value = close()
    if inspect.isawaitable(value):
        await value


__all__ = [
    "EnvironmentCredentialBroker",
    "RuntimeComposition",
    "build_upload_runtime",
    "build_runtime",
    "build_olist_runtime",
]
