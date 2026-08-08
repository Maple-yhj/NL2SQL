from __future__ import annotations

import json
from datetime import UTC, datetime

from data_agent.analysis_agent.models import (
    AgentArtifactKind,
    AgentArtifactRef,
    AgentContextSnapshot,
    AgentObservation,
    AnalysisGoal,
    AnalysisPlan,
    AnalysisStep,
    DatasetAuthority,
    EvidenceRef,
)


class SequenceModel:
    model_id = "fake-structured-model"
    version = "test"

    def __init__(self, responses: list[str | dict[str, object]]) -> None:
        self.responses = [
            item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            for item in responses
        ]
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "max_output_tokens": max_output_tokens,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected model call")
        return self.responses.pop(0)


def authority(mode: str = "execute") -> DatasetAuthority:
    return DatasetAuthority(
        tenant_id="tenant-1",
        user_id="user-1",
        source_id="orders-source",
        source_version=2,
        binding_id="orders-binding",
        binding_version=3,
        schema_fingerprint="sha256:" + "a" * 64,
        allowed_relation_ids=("public.orders",),
        mode=mode,
    )


def goal() -> AnalysisGoal:
    return AnalysisGoal(
        original_question="What is total revenue?",
        contextualized_question="What is total revenue for the selected orders dataset?",
        requested_output="grounded answer",
        success_criteria=("Return total revenue with evidence",),
    )


def plan(*, revision: int = 1, status: str = "pending") -> AnalysisPlan:
    return AnalysisPlan(
        plan_id="analysis-plan",
        revision=revision,
        steps=(
            AnalysisStep(
                step_id="query",
                objective="Compute total revenue",
                status=status,
                expected_evidence=("total_revenue",),
            ),
        ),
        completion_criteria=("Total revenue has validated evidence",),
    )


def context(**updates: object) -> AgentContextSnapshot:
    values: dict[str, object] = {
        "catalog_digest": "b" * 64,
        "binding_digest": "c" * 64,
        "catalog_summary": {"relations": ["orders"]},
        "semantic_summary": {"fields": ["dataset.orders.amount"]},
        "conversation_summary": "The user selected the orders dataset.",
        "allowed_tool_names": ("catalog.inspect", "query.compile"),
    }
    values.update(updates)
    return AgentContextSnapshot.model_validate(values)


def artifact(**updates: object) -> AgentArtifactRef:
    values: dict[str, object] = {
        "artifact_id": "artifact-result",
        "run_id": "run-1",
        "kind": AgentArtifactKind.QUERY_RESULT,
        "digest": "d" * 64,
        "schema_digest": "sha256:" + "a" * 64,
        "row_count": 1,
        "sensitivity": "row_data",
        "created_at": datetime(2026, 8, 8, tzinfo=UTC),
    }
    values.update(updates)
    return AgentArtifactRef.model_validate(values)


def evidence(**updates: object) -> EvidenceRef:
    values: dict[str, object] = {
        "evidence_id": "evidence-total",
        "claim_key": "total_revenue",
        "artifact_id": "artifact-result",
        "source_id": "orders-source",
        "source_version": 2,
        "binding_id": "orders-binding",
        "binding_version": 3,
        "schema_fingerprint": "sha256:" + "a" * 64,
        "sql_digest": "e" * 64,
        "result_digest": "d" * 64,
        "field_refs": ("total_revenue",),
    }
    values.update(updates)
    return EvidenceRef.model_validate(values)


def observation(**updates: object) -> AgentObservation:
    item = artifact()
    proof = evidence()
    values: dict[str, object] = {
        "observation_id": "observation-result",
        "action_id": "action-query",
        "tool_name": "query.execute",
        "status": "succeeded",
        "summary": "Returned one aggregate row",
        "artifact_refs": (item,),
        "evidence_refs": (proof,),
        "safe_preview": ({"total_revenue": 42.0},),
    }
    values.update(updates)
    return AgentObservation.model_validate(values)
