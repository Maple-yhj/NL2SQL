"""Public contracts for the native user-dataset Agent runtime."""

from importlib import import_module

from .contracts import ConversationRuntime, DataAgentRuntime, ProductRuntime
from .errors import AgentError, ErrorCode
from .events import AgentEvent, AgentEventPayload, AgentEventType
from .models import (
    AgentArtifactSummary,
    AgentMode,
    AgentRequest,
    AgentResponse,
    AgentRow,
    AgentTraceEntry,
    AnalysisStepSummary,
    ChartSpec,
    ComponentVersionPin,
    ConversationMessage,
    ConversationMessageMetadata,
    ConversationSummary,
    DatasetRuntimeVersionPins,
    EvidenceSummary,
    PrincipalContext,
    ProposalSummary,
    RunBudget,
    RuntimeVersionPins,
)
_LAZY_EXPORTS = {
    "build_analysis_agent_runtime": (
        ".composition_root",
        "build_analysis_agent_runtime",
    ),
    "build_upload_runtime": (".composition_root", "build_upload_runtime"),
    "USER_DATASET_DOMAIN_ID": (".upload_runtime", "USER_DATASET_DOMAIN_ID"),
    "USER_DATASET_ENTERPRISE_ID": (".upload_runtime", "USER_DATASET_ENTERPRISE_ID"),
    "SQLiteConversationRepository": (
        ".upload_runtime",
        "SQLiteConversationRepository",
    ),
    "UploadDatasetRuntime": (".upload_runtime", "UploadDatasetRuntime"),
    "UploadRuntimeComposition": (".upload_runtime", "UploadRuntimeComposition"),
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
    "AgentArtifactSummary",
    "AgentError",
    "AgentEvent",
    "AgentEventPayload",
    "AgentEventType",
    "AgentMode",
    "AgentRequest",
    "AgentResponse",
    "AgentRow",
    "AgentTraceEntry",
    "AnalysisStepSummary",
    "ChartSpec",
    "ComponentVersionPin",
    "ConversationMessage",
    "ConversationMessageMetadata",
    "ConversationRuntime",
    "ConversationSummary",
    "DataAgentRuntime",
    "DatasetRuntimeVersionPins",
    "ErrorCode",
    "EvidenceSummary",
    "PrincipalContext",
    "ProductRuntime",
    "ProposalSummary",
    "RunBudget",
    "RuntimeVersionPins",
    "SQLiteConversationRepository",
    "USER_DATASET_DOMAIN_ID",
    "USER_DATASET_ENTERPRISE_ID",
    "UploadDatasetRuntime",
    "UploadRuntimeComposition",
    "build_analysis_agent_runtime",
    "build_upload_runtime",
]
