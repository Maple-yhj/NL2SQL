from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from data_agent.analysis_agent.models import (
    AgentAction,
    AgentAnswerDraft,
    AgentArtifactKind,
    AgentArtifactRef,
    AgentInputReason,
    AgentInputRequest,
    AgentObservation,
    AnalysisGoal,
    AnalysisPlan,
    AnalysisStep,
    DatasetAuthority,
    EvaluationDecision,
    EvidenceRef,
    FindingDraft,
    PlannerDecision,
    stable_digest,
)


def authority(**overrides: object) -> DatasetAuthority:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "source_id": "orders",
        "source_version": 3,
        "binding_id": "orders-binding",
        "binding_version": 4,
        "schema_fingerprint": "a" * 64,
        "allowed_relation_ids": ("orders", "customers"),
        "mode": "execute",
    }
    values.update(overrides)
    return DatasetAuthority.model_validate(values)


def plan() -> AnalysisPlan:
    return AnalysisPlan(
        plan_id="plan-1",
        revision=1,
        steps=(
            AnalysisStep(
                step_id="inspect",
                objective="Inspect the catalog",
                status="pending",
            ),
            AnalysisStep(
                step_id="query",
                objective="Query revenue",
                status="pending",
                depends_on=("inspect",),
                expected_evidence=("revenue",),
            ),
        ),
        completion_criteria=("Revenue is supported by evidence",),
    )


def artifact() -> AgentArtifactRef:
    return AgentArtifactRef(
        artifact_id="artifact-1",
        run_id="run-1",
        kind=AgentArtifactKind.QUERY_RESULT,
        digest="b" * 64,
        schema_digest="c" * 64,
        row_count=1,
        sensitivity="derived",
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
    )


def evidence() -> EvidenceRef:
    return EvidenceRef(
        evidence_id="evidence-1",
        claim_key="total_revenue",
        artifact_id="artifact-1",
        source_id="orders",
        source_version=3,
        binding_id="orders-binding",
        binding_version=4,
        schema_fingerprint="a" * 64,
        sql_digest="d" * 64,
        result_digest="b" * 64,
        field_refs=("revenue",),
    )


def test_dataset_authority_validates_pins_identity_mode_and_allowlist() -> None:
    value = authority()
    assert value.allowed_relation_ids == ("orders", "customers")
    assert value.mode.value == "execute"
    assert authority(schema_fingerprint="sha256:" + "b" * 64).schema_fingerprint.startswith(
        "sha256:"
    )

    for updates in (
        {"tenant_id": " "},
        {"source_version": 0},
        {"schema_fingerprint": "not-a-digest"},
        {"allowed_relation_ids": ()},
        {"allowed_relation_ids": ("orders", "orders")},
        {"mode": "unsafe"},
    ):
        with pytest.raises(ValidationError):
            authority(**updates)


def test_analysis_goal_and_plan_are_strict_revisioned_dags() -> None:
    goal = AnalysisGoal(
        original_question="Revenue?",
        contextualized_question="Revenue for 2026?",
        requested_output="answer",
        success_criteria=("Return total revenue",),
    )
    assert goal.constraints == ()
    assert plan().steps[1].depends_on == ("inspect",)

    with pytest.raises(ValidationError):
        AnalysisGoal(
            original_question="Revenue?",
            contextualized_question="Revenue?",
            requested_output="answer",
            success_criteria=("Return revenue",),
            prompt="leak",
        )
    with pytest.raises(ValidationError, match="unique"):
        AnalysisPlan(
            plan_id="plan-1",
            revision=1,
            steps=(
                AnalysisStep(step_id="same", objective="one", status="pending"),
                AnalysisStep(step_id="same", objective="two", status="pending"),
            ),
            completion_criteria=("done",),
        )
    with pytest.raises(ValidationError, match="cycle"):
        AnalysisPlan(
            plan_id="plan-1",
            revision=1,
            steps=(
                AnalysisStep(
                    step_id="one",
                    objective="one",
                    status="pending",
                    depends_on=("two",),
                ),
                AnalysisStep(
                    step_id="two",
                    objective="two",
                    status="pending",
                    depends_on=("one",),
                ),
            ),
            completion_criteria=("done",),
        )
    with pytest.raises(ValidationError):
        AnalysisPlan(
            plan_id="plan-1",
            revision=0,
            steps=(AnalysisStep(step_id="one", objective="one", status="pending"),),
            completion_criteria=("done",),
        )


def test_agent_action_rejects_authority_and_executable_escape_hatches() -> None:
    action = AgentAction(
        action_id="action-1",
        tool_name="query.compile",
        arguments={"metric_refs": ["dataset.orders.revenue"]},
        purpose="Compile a governed query",
        expected_evidence=("prepared query",),
    )
    assert action.arguments["metric_refs"] == ["dataset.orders.revenue"]

    for tool_name in ("query", "Query.compile", "query/compile", "query..compile"):
        with pytest.raises(ValidationError):
            AgentAction(
                action_id="action-1",
                tool_name=tool_name,
                arguments={},
                purpose="bad",
                expected_evidence=(),
            )
    for arguments in (
        {"tenant_id": "other"},
        {"nested": {"source_id": "other"}},
        {"raw_sql": "drop table x"},
        {"code": "import os"},
        {"file_path": "/etc/passwd"},
        {"dsn": "postgresql://secret"},
    ):
        with pytest.raises(ValidationError, match="forbidden"):
            AgentAction(
                action_id="action-1",
                tool_name="query.compile",
                arguments=arguments,
                purpose="bad",
                expected_evidence=(),
            )


def test_artifact_evidence_and_observation_enforce_integrity() -> None:
    observation = AgentObservation(
        observation_id="observation-1",
        action_id="action-1",
        tool_name="query.execute",
        status="succeeded",
        summary="One row returned",
        artifact_refs=(artifact(),),
        evidence_refs=(evidence(),),
        safe_preview=({"revenue": 42},),
    )
    assert observation.evidence_refs[0].result_digest == artifact().digest

    with pytest.raises(ValidationError, match="error"):
        AgentObservation(
            observation_id="observation-1",
            action_id="action-1",
            tool_name="query.execute",
            status="failed",
            summary="failed",
        )
    with pytest.raises(ValidationError, match="artifact"):
        AgentObservation(
            observation_id="observation-1",
            action_id="action-1",
            tool_name="query.execute",
            status="succeeded",
            summary="bad ref",
            artifact_refs=(artifact(),),
            evidence_refs=(evidence().model_copy(update={"artifact_id": "other"}),),
        )


def test_planner_decision_discriminator_fields_are_mutually_exclusive() -> None:
    action = AgentAction(
        action_id="action-1",
        tool_name="catalog.inspect",
        arguments={},
        purpose="Inspect catalog",
        expected_evidence=("catalog",),
    )
    decision = PlannerDecision(
        plan=plan(),
        decision="act",
        next_action=action,
        rationale_summary="Catalog evidence is needed first",
    )
    assert decision.next_action == action

    clarification = AgentInputRequest(
        interrupt_id="interrupt-1",
        reason=AgentInputReason.CLARIFICATION,
        prompt="Which date field should be used?",
    )
    with pytest.raises(ValidationError, match="clarification"):
        PlannerDecision(
            plan=plan(),
            decision="act",
            next_action=action,
            clarification=clarification,
            rationale_summary="invalid",
        )
    with pytest.raises(ValidationError, match="next_action"):
        PlannerDecision(
            plan=plan(),
            decision="finish",
            next_action=action,
            completion_summary="done",
            rationale_summary="invalid",
        )


def test_evaluation_and_answer_draft_require_grounding() -> None:
    with pytest.raises(ValidationError, match="clarification"):
        EvaluationDecision(
            decision="clarify",
            evidence_sufficient=False,
            completed_step_ids=(),
            missing_evidence=("date field",),
            contradictions=(),
            rationale_summary="Need user input",
        )

    draft = AgentAnswerDraft(
        answer="Revenue is 42.",
        key_findings=(
            FindingDraft(
                finding_id="revenue",
                claim="Revenue is 42",
                evidence_ids=("evidence-1",),
            ),
        ),
        limitations=(),
        evidence_ids=("evidence-1",),
    )
    assert draft.evidence_ids == ("evidence-1",)

    with pytest.raises(ValidationError, match="numeric"):
        AgentAnswerDraft(
            answer="Revenue is 42.",
            key_findings=(
                FindingDraft(
                    finding_id="revenue",
                    claim="Revenue is 42",
                    evidence_ids=(),
                ),
            ),
            limitations=(),
            evidence_ids=(),
        )


def test_stable_digest_uses_canonical_json() -> None:
    left = stable_digest({"b": [2, 3], "a": 1})
    right = stable_digest({"a": 1, "b": [2, 3]})
    assert left == right
    assert len(left) == 64
    assert left != stable_digest({"a": 1, "b": [3, 2]})


def test_public_models_do_not_allow_unchecked_construct() -> None:
    with pytest.raises(ValidationError):
        DatasetAuthority.model_construct(
            tenant_id="tenant-1",
            user_id="user-1",
            source_id="orders",
            source_version=0,
            binding_id="binding",
            binding_version=1,
            schema_fingerprint="bad",
            allowed_relation_ids=(),
            mode="execute",
        )
