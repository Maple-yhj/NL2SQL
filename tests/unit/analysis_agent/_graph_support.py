from __future__ import annotations

from datetime import UTC, datetime

from data_agent.analysis_agent.graph import build_dataset_version_pins
from data_agent.analysis_agent.models import (
    AgentAction,
    AgentAnswerDraft,
    AgentArtifactKind,
    AgentArtifactRef,
    AgentObservation,
    AnalysisPlan,
    AnalysisStep,
    EvidenceRef,
    EvaluationDecision,
    FindingDraft,
    PlannerDecision,
    stable_digest,
)
from data_agent.analysis_agent.nodes import AnalysisGraphContext
from data_agent.public_contracts import AgentError, ErrorCode
from data_agent.runtime.models import AgentMode, ComponentVersionPin
from data_agent.tools.providers.dataset import build_dataset_tool_registry

from ._decision_support import authority, context


def analysis_plan(
    *statuses: str,
    revision: int = 1,
) -> AnalysisPlan:
    if not statuses:
        statuses = ("pending",)
    steps = tuple(
        AnalysisStep(
            step_id=f"step-{index}",
            objective=f"Analysis step {index}",
            status=status,
            depends_on=((f"step-{index - 1}",) if index > 1 else ()),
            expected_evidence=(f"claim_{index}",),
        )
        for index, status in enumerate(statuses, start=1)
    )
    return AnalysisPlan(
        plan_id="graph-plan",
        revision=revision,
        steps=steps,
        completion_criteria=("Every step has evidence",),
    )


def action(
    action_id: str,
    *,
    tool_name: str = "query.preview",
) -> AgentAction:
    arguments: dict[str, object]
    if tool_name in {"query.preview", "query.execute", "query.explain", "data.profile"}:
        arguments = {"artifact_id": "artifact-" + "a" * 64, "preview_rows": 20}
    elif tool_name == "catalog.inspect":
        arguments = {}
    else:
        arguments = {"artifact_id": "artifact-" + "a" * 64}
    return AgentAction(
        action_id=action_id,
        tool_name=tool_name,
        arguments=arguments,
        purpose="Run one governed analysis action",
        expected_evidence=("validated result",),
    )


def act_decision(
    *,
    action_id: str,
    plan_value: AnalysisPlan,
    tool_name: str = "query.preview",
) -> PlannerDecision:
    return PlannerDecision(
        plan=plan_value,
        decision="act",
        next_action=action(action_id, tool_name=tool_name),
        rationale_summary="Run the next bounded tool.",
    )


def finish_decision(plan_value: AnalysisPlan) -> PlannerDecision:
    return PlannerDecision(
        plan=plan_value,
        decision="finish",
        completion_summary="The analysis plan is complete.",
        rationale_summary="No additional action is required.",
    )


class SequencePlanner:
    def __init__(self, decisions: list[PlannerDecision | Exception]) -> None:
        self.decisions = decisions
        self.calls: list[dict[str, object]] = []

    async def decide(self, **kwargs: object) -> PlannerDecision:
        self.calls.append(kwargs)
        if not self.decisions:
            raise AssertionError("unexpected planner call")
        value = self.decisions.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class SequenceEvaluator:
    def __init__(self, decisions: list[EvaluationDecision] | None = None) -> None:
        self.decisions = decisions or []
        self.calls: list[dict[str, object]] = []

    async def evaluate(self, **kwargs: object) -> EvaluationDecision:
        self.calls.append(kwargs)
        if self.decisions:
            return self.decisions.pop(0)
        observations = kwargs["observations"]
        latest = observations[-1]
        if latest.status == "failed" or any(
            item.row_count == 0 for item in latest.artifact_refs
        ):
            return EvaluationDecision(
                decision="replan",
                evidence_sufficient=False,
                completed_step_ids=(),
                missing_evidence=("successful non-empty result",),
                contradictions=(),
                rationale_summary="The last action needs correction.",
            )
        current_plan = kwargs["plan"]
        return EvaluationDecision(
            decision="finish",
            evidence_sufficient=True,
            completed_step_ids=tuple(step.step_id for step in current_plan.steps),
            missing_evidence=(),
            contradictions=(),
            rationale_summary="The result is sufficient.",
        )


class GroundedSynthesizer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def synthesize(self, **kwargs: object) -> AgentAnswerDraft:
        self.calls.append(kwargs)
        evidence = kwargs["evidence"]
        evidence_ids = tuple(item.evidence_id for item in evidence)
        return AgentAnswerDraft(
            answer="The governed result is supported by evidence.",
            key_findings=(
                FindingDraft(
                    finding_id="finding-result",
                    claim="The governed result is supported.",
                    evidence_ids=evidence_ids,
                ),
            ),
            recommended_chart_artifact_id=None,
            limitations=(),
            evidence_ids=evidence_ids,
        )


class SequenceToolExecutor:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, object]] = []

    async def invoke(self, **kwargs: object) -> AgentObservation:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        state = kwargs["state"]
        guarded_action = kwargs["action"]
        index = len(self.calls)
        observation_id = f"observation-{index}"
        if outcome == "failed":
            return AgentObservation(
                observation_id=observation_id,
                action_id=guarded_action.action_id,
                tool_name=guarded_action.tool_name,
                status="failed",
                summary="Retryable governed tool failure",
                error=AgentError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="safe tool failure",
                    retryable=True,
                ),
            )
        row_count = 0 if outcome == "empty" else 1
        digest = stable_digest(
            {"run": state["run_id"], "action": guarded_action.action_id, "index": index}
        )
        artifact_id = f"artifact-result-{index}"
        item = AgentArtifactRef(
            artifact_id=artifact_id,
            run_id=state["run_id"],
            kind=AgentArtifactKind.QUERY_PREVIEW,
            digest=digest,
            schema_digest=state["authority"].schema_fingerprint,
            row_count=row_count,
            sensitivity="row_data",
            created_at=datetime(2026, 8, 8, tzinfo=UTC),
        )
        proof = EvidenceRef(
            evidence_id=f"evidence-{index}",
            claim_key=f"claim_{index}",
            artifact_id=artifact_id,
            source_id=state["authority"].source_id,
            source_version=state["authority"].source_version,
            binding_id=state["authority"].binding_id,
            binding_version=state["authority"].binding_version,
            schema_fingerprint=state["authority"].schema_fingerprint,
            result_digest=digest,
            field_refs=(f"claim_{index}",),
        )
        return AgentObservation(
            observation_id=observation_id,
            action_id=guarded_action.action_id,
            tool_name=guarded_action.tool_name,
            status="succeeded",
            summary="Governed preview completed",
            artifact_refs=(item,),
            evidence_refs=(proof,),
            safe_preview=(({} if row_count == 0 else {"value": index}),),
        )


def graph_context(
    *,
    mode: AgentMode,
    planner: SequencePlanner,
    evaluator: SequenceEvaluator | None = None,
    tools: SequenceToolExecutor | None = None,
    synthesizer: GroundedSynthesizer | None = None,
    budget_limits=None,
    clock=None,
    cancelled=None,
) -> AnalysisGraphContext:
    registry = build_dataset_tool_registry()
    auth = authority(mode.value)
    pins = build_dataset_version_pins(
        authority=auth,
        tool_registry_version=registry.version,
        model_versions=(ComponentVersionPin(component="planner", version="fake"),),
    )

    async def loader(state):
        del state
        return context(allowed_tool_names=registry.names())

    values = {
        "planner": planner,
        "evaluator": evaluator or SequenceEvaluator(),
        "synthesizer": synthesizer or GroundedSynthesizer(),
        "tool_executor": tools or SequenceToolExecutor(["success"]),
        "context_loader": loader,
        "tool_specs": registry.specs(),
        "version_pins": pins,
    }
    if budget_limits is not None:
        values["budget_limits"] = budget_limits
    if clock is not None:
        values["clock"] = clock
    if cancelled is not None:
        values["cancelled"] = cancelled
    return AnalysisGraphContext(**values)
