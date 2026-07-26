"""Runtime contracts shared by Data Agent adapters and implementations."""

from importlib import import_module

from .composition import (
    ResolvedRuntimeBundle,
    canonical_json,
    compile_runtime_bundle,
    load_bundle_manifest,
    write_bundle_manifest,
    stable_digest,
)
from .contracts import ConversationRuntime, DataAgentRuntime, ProductRuntime
from .errors import AgentError, ErrorCode
from .events import AgentEvent, AgentEventType
from .models import (
    AgentMode,
    AgentRequest,
    AgentResponse,
    ChartSpec,
    ConversationMessage,
    ConversationMessageMetadata,
    ConversationSummary,
    PrincipalContext,
    RunBudget,
)
from .packs import DeploymentProfile, DomainPack, EnterpriseDataBinding
from .profile_loader import (
    PackLoadError,
    export_pack_schemas,
    load_domain_pack,
    load_enterprise_binding,
    compile_profile_bundle,
    load_pack_yaml,
)
from .schema_catalog import (
    load_schema_catalog,
    schema_fingerprint,
    validate_enterprise_binding_schema,
)
from .bundle_store import (
    BundleAttestations,
    BundleNotActiveError,
    BundlePaths,
    BundleSnapshot,
    BundleStore,
    BundleSourceId,
    SourceAttestation,
    VerifiedBundleCandidate,
)
from .context import (
    CONTEXT_PRECEDENCE,
    ContextAssembler,
    ContextBudget,
    ContextBudgetExceededError,
    ContextEnvelope,
    ContextItem,
    ContextOwner,
    ContextSource,
    ContextVersionPins,
    SecurityContext,
)


_LAZY_EXPORTS = {
    "bundle_paths": (".paths", "bundle_paths"),
    "compile_packs": (".maintenance", "compile_packs"),
    "EnvironmentCredentialBroker": (".composition_root", "EnvironmentCredentialBroker"),
    "RuntimeComposition": (".composition_root", "RuntimeComposition"),
    "build_runtime": (".composition_root", "build_runtime"),
    "build_olist_runtime": (".composition_root", "build_olist_runtime"),
    "RuntimeContextResolver": (".context_resolver", "RuntimeContextResolver"),
    "LogicalPlanner": (".dependencies", "LogicalPlanner"),
    "MemoryProposalFactory": (".dependencies", "MemoryProposalFactory"),
    "ModelClient": (".dependencies", "ModelClient"),
    "NoMemoryProposals": (".dependencies", "NoMemoryProposals"),
    "RuntimeDependencies": (".dependencies", "RuntimeDependencies"),
    "RuntimeGraphExecutor": (".dependencies", "RuntimeGraphExecutor"),
    "RuntimeVersionPins": (".dependencies", "RuntimeVersionPins"),
    "rebuild_semantic_index": (".maintenance", "rebuild_semantic_index"),
    "resolve_project_root": (".paths", "resolve_project_root"),
    "resolve_bundle_paths": (".paths", "resolve_bundle_paths"),
    "resolve_source_root": (".paths", "resolve_source_root"),
    "ModelLogicalPlanner": (".planner", "ModelLogicalPlanner"),
    "DataAgentRuntimeService": (".service", "DataAgentRuntimeService"),
    "DefaultDataAgentRuntime": (".service", "DefaultDataAgentRuntime"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value

__all__ = [
    "AgentError",
    "AgentEvent",
    "AgentEventType",
    "AgentMode",
    "AgentRequest",
    "AgentResponse",
    "ChartSpec",
    "ConversationMessage",
    "ConversationMessageMetadata",
    "ConversationSummary",
    "DataAgentRuntime",
    "ConversationRuntime",
    "DeploymentProfile",
    "DomainPack",
    "EnterpriseDataBinding",
    "ErrorCode",
    "PackLoadError",
    "PrincipalContext",
    "ProductRuntime",
    "ResolvedRuntimeBundle",
    "RunBudget",
    "canonical_json",
    "compile_runtime_bundle",
    "load_bundle_manifest",
    "write_bundle_manifest",
    "export_pack_schemas",
    "load_domain_pack",
    "load_enterprise_binding",
    "compile_profile_bundle",
    "load_pack_yaml",
    "load_schema_catalog",
    "schema_fingerprint",
    "stable_digest",
    "validate_enterprise_binding_schema",
    "BundleNotActiveError",
    "BundleAttestations",
    "BundlePaths",
    "BundleSnapshot",
    "BundleStore",
    "BundleSourceId",
    "SourceAttestation",
    "VerifiedBundleCandidate",
    "CONTEXT_PRECEDENCE",
    "ContextAssembler",
    "ContextBudget",
    "ContextBudgetExceededError",
    "ContextEnvelope",
    "ContextItem",
    "ContextOwner",
    "ContextSource",
    "ContextVersionPins",
    "DataAgentRuntimeService",
    "DefaultDataAgentRuntime",
    "EnvironmentCredentialBroker",
    "LogicalPlanner",
    "MemoryProposalFactory",
    "ModelClient",
    "ModelLogicalPlanner",
    "NoMemoryProposals",
    "RuntimeComposition",
    "RuntimeContextResolver",
    "RuntimeDependencies",
    "RuntimeGraphExecutor",
    "RuntimeVersionPins",
    "SecurityContext",
    "build_olist_runtime",
    "build_runtime",
    "bundle_paths",
    "compile_packs",
    "rebuild_semantic_index",
    "resolve_project_root",
    "resolve_bundle_paths",
    "resolve_source_root",
]
