"""Composition root for the exact six stable Tool providers."""

from __future__ import annotations

from data_agent.runtime.binding import BindingCompiler, SqlDialect
from data_agent.runtime.composition import ResolvedRuntimeBundle
from data_agent.runtime.packs import DomainPack, EnterpriseDataBinding
from data_agent.tools.connectors import DataSourceConnector

from ..registry import ToolRegistry
from .answer import AnswerRenderProvider
from .evidence import EvidenceSigner
from .inspect import DataInspectProvider
from .profile import ResultProfileProvider
from .query import QueryCompileProvider, QueryExecuteProvider
from .semantic import SemanticSearchProvider


BUILTIN_TOOL_NAMES = (
    "semantic.search",
    "data.inspect",
    "query.compile",
    "query.execute",
    "result.profile",
    "answer.render",
)


def build_builtin_registry(
    domain_pack: DomainPack,
    enterprise_binding: EnterpriseDataBinding,
    bundle: ResolvedRuntimeBundle,
    connector: DataSourceConnector,
    *,
    dialect: SqlDialect = "postgres",
) -> ToolRegistry:
    if bundle.tool_registry_version != "1.0.0":
        raise ValueError("resolved bundle requests an unsupported Tool Registry version")
    capabilities = getattr(connector, "capabilities", None)
    if callable(capabilities):
        if capabilities().dialect != dialect:
            raise ValueError("connector dialect does not match the binding compiler")
    elif dialect != "postgres":
        raise ValueError("non-PostgreSQL connectors must declare capabilities")
    compiler = BindingCompiler(
        domain_pack,
        enterprise_binding,
        bundle,
        dialect=dialect,
    )
    evidence_signer = EvidenceSigner()
    providers = (
        SemanticSearchProvider(domain_pack),
        DataInspectProvider(connector),
        QueryCompileProvider(compiler),
        QueryExecuteProvider(connector, compiler, evidence_signer),
        ResultProfileProvider(evidence_signer),
        AnswerRenderProvider(evidence_signer),
    )
    registry = ToolRegistry(version="1.0.0")
    for expected_name, provider in zip(BUILTIN_TOOL_NAMES, providers, strict=True):
        if provider.spec.name != expected_name:
            raise ValueError("built-in provider order or manifest drifted")
        registry.register(provider.spec, provider)
    registry.freeze()
    if registry.names() != BUILTIN_TOOL_NAMES:
        raise ValueError("built-in registry must contain exactly six stable tools")
    return registry
