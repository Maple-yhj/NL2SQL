"""Direct, inert LangGraph Studio export for the native analysis Agent."""

from __future__ import annotations

from datetime import UTC, datetime

from data_agent.analysis_agent.graph import (
    build_analysis_agent_graph,
    build_dataset_version_pins,
)
from data_agent.analysis_agent.models import (
    AgentContextSnapshot,
    AnalysisPlan,
    AnalysisStep,
    DatasetAuthority,
    PlannerDecision,
    stable_digest,
)
from data_agent.analysis_agent.nodes import AnalysisGraphContext
from data_agent.runtime.models import AgentMode, AgentRequest, ComponentVersionPin
from data_agent.tools.providers.dataset import build_dataset_tool_registry


_DEVELOPMENT_DIGEST = stable_digest({"composition": "langgraph-studio-development"})
_DEVELOPMENT_AUTHORITY = DatasetAuthority(
    tenant_id="studio-tenant",
    user_id="studio-user",
    source_id="studio-source",
    source_version=1,
    binding_id="studio-binding",
    binding_version=1,
    schema_fingerprint=_DEVELOPMENT_DIGEST,
    allowed_relation_ids=("studio.example",),
    mode=AgentMode.PLAN,
)


class _DevelopmentPlanner:
    async def decide(self, **_kwargs: object) -> PlannerDecision:
        return PlannerDecision(
            plan=AnalysisPlan(
                plan_id="studio-development-plan",
                revision=1,
                steps=(
                    AnalysisStep(
                        step_id="inspect-selected-dataset",
                        objective="Inspect the selected dataset with governed tools",
                        status="skipped",
                        expected_evidence=("validated dataset evidence",),
                    ),
                ),
                completion_criteria=("A governed execution plan is available",),
            ),
            decision="finish",
            completion_summary=(
                "Studio loaded the native Agent topology in offline plan mode. "
                "Use the API or CLI with a pinned dataset to execute analysis."
            ),
            rationale_summary="The Studio development composition performs no external I/O.",
        )


class _UnexpectedEvaluator:
    async def evaluate(self, **_kwargs: object):
        raise RuntimeError("the offline Studio plan does not evaluate tool results")


class _UnexpectedSynthesizer:
    async def synthesize(self, **_kwargs: object):
        raise RuntimeError("the offline Studio plan does not call a model")


class _UnexpectedToolExecutor:
    async def invoke(self, **_kwargs: object):
        raise RuntimeError("the offline Studio plan does not execute tools")


def _development_context() -> AnalysisGraphContext:
    registry = build_dataset_tool_registry()

    async def load_context(_state):
        return AgentContextSnapshot(
            catalog_digest=_DEVELOPMENT_DIGEST,
            binding_digest=_DEVELOPMENT_DIGEST,
            catalog_summary={"composition": "offline Studio example"},
            semantic_summary={"execution": "disabled"},
            allowed_tool_names=registry.names(),
        )

    pins = build_dataset_version_pins(
        authority=_DEVELOPMENT_AUTHORITY,
        tool_registry_version=registry.version,
        model_versions=(
            ComponentVersionPin(component="planner", version="studio-offline"),
            ComponentVersionPin(component="evaluator", version="studio-offline"),
            ComponentVersionPin(component="synthesizer", version="studio-offline"),
        ),
    )
    return AnalysisGraphContext(
        planner=_DevelopmentPlanner(),
        evaluator=_UnexpectedEvaluator(),
        synthesizer=_UnexpectedSynthesizer(),
        tool_executor=_UnexpectedToolExecutor(),
        context_loader=load_context,
        tool_specs=registry.specs(),
        version_pins=pins,
        clock=lambda: datetime(2026, 8, 8, tzinfo=UTC),
    )


def development_input() -> dict[str, object]:
    """Return a safe example that is directly runnable in Studio."""

    authority = _DEVELOPMENT_AUTHORITY
    return {
        "run_id": "studio-development-run",
        "request": AgentRequest(
            question="Create a governed analysis plan for the selected dataset",
            source_id=authority.source_id,
            source_version=authority.source_version,
            binding_id=authority.binding_id,
            binding_version=authority.binding_version,
            mode=authority.mode,
        ),
        "authority": authority,
    }


def build_studio_graph(*, context: AnalysisGraphContext | None = None, checkpointer=None):
    """Return the native graph bound to an inert development composition."""

    return build_analysis_agent_graph(
        checkpointer=checkpointer,
        default_context=context or _development_context(),
    ).compiled_graph


graph = build_studio_graph()


__all__ = ["build_studio_graph", "development_input", "graph"]
