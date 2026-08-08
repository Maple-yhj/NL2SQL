"""Native, governed Data Analysis Agent contracts and runtime components.

Imports stay lazy so the public runtime models may reference Agent contracts
without triggering the checkpoint state module during package initialization.
"""

from importlib import import_module


_STATE_EXPORTS = {"AnalysisAgentState"}
_PLANNER_EXPORTS = {"AnalysisPlanner"}
_EVALUATOR_EXPORTS = {"AnalysisEvaluator"}
_SYNTHESIZER_EXPORTS = {"AnalysisSynthesizer"}
_GRAPH_EXPORTS = {
    "ANALYSIS_GRAPH_DIGEST",
    "ANALYSIS_GRAPH_ID",
    "ANALYSIS_GRAPH_VERSION",
    "CompiledAnalysisGraph",
    "build_analysis_agent_graph",
    "build_dataset_version_pins",
}
_NODE_EXPORTS = {"AnalysisGraphContext", "DatasetAgentToolInvoker"}
_CHECKPOINT_EXPORTS = {
    "CheckpointerFactory",
    "CheckpointerResource",
    "InMemoryCheckpointerFactory",
    "PostgresCheckpointerFactory",
    "SQLiteCheckpointerFactory",
    "checkpoint_serializer",
}
_RUNTIME_EXPORTS = {
    "AgentResumeRequest",
    "AnalysisRunResolver",
    "AnalysisRuntimeError",
    "DataAnalysisAgentRuntime",
    "ResolvedAnalysisRun",
}
_COMPOSITION_EXPORTS = {
    "AnalysisRuntimeComposition",
    "DataSourceAuthorityService",
    "DatasetAnalysisRunResolver",
    "build_analysis_agent_runtime",
    "build_analysis_runtime_from_resolver",
}


__all__ = [
    "AgentAction",
    "AgentAnswerDraft",
    "AgentArtifactKind",
    "AgentArtifactRef",
    "AgentBudgetState",
    "AgentContextSnapshot",
    "AgentInputReason",
    "AgentInputRequest",
    "AgentObservation",
    "AgentRunBudget",
    "AgentStatus",
    "AgentResumeRequest",
    "ANALYSIS_GRAPH_DIGEST",
    "ANALYSIS_GRAPH_ID",
    "ANALYSIS_GRAPH_VERSION",
    "AnalysisAgentState",
    "AnalysisGoal",
    "AnalysisGraphContext",
    "AnalysisRunResolver",
    "AnalysisRuntimeComposition",
    "AnalysisRuntimeError",
    "AnalysisEvaluator",
    "AnalysisPlanner",
    "AnalysisPlan",
    "AnalysisSynthesizer",
    "CompiledAnalysisGraph",
    "CheckpointerFactory",
    "CheckpointerResource",
    "DataAnalysisAgentRuntime",
    "DataSourceAuthorityService",
    "AnalysisStep",
    "DatasetAuthority",
    "DatasetAnalysisRunResolver",
    "DatasetAgentToolInvoker",
    "EvaluationDecision",
    "EvidenceRef",
    "FindingDraft",
    "PlannerDecision",
    "InMemoryCheckpointerFactory",
    "PostgresCheckpointerFactory",
    "ResolvedAnalysisRun",
    "SQLiteCheckpointerFactory",
    "build_analysis_agent_graph",
    "build_analysis_agent_runtime",
    "build_analysis_runtime_from_resolver",
    "build_dataset_version_pins",
    "checkpoint_serializer",
    "stable_digest",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    if name in _STATE_EXPORTS:
        module_name = ".state"
    elif name in _PLANNER_EXPORTS:
        module_name = ".planner"
    elif name in _EVALUATOR_EXPORTS:
        module_name = ".evaluator"
    elif name in _SYNTHESIZER_EXPORTS:
        module_name = ".synthesizer"
    elif name in _GRAPH_EXPORTS:
        module_name = ".graph"
    elif name in _NODE_EXPORTS:
        module_name = ".nodes"
    elif name in _CHECKPOINT_EXPORTS:
        module_name = ".checkpoints"
    elif name in _RUNTIME_EXPORTS:
        module_name = ".runtime"
    elif name in _COMPOSITION_EXPORTS:
        module_name = ".composition"
    else:
        module_name = ".models"
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
