from __future__ import annotations

from datetime import UTC, datetime

import pytest

from data_agent.analysis_agent.models import (
    AgentArtifactRef,
    AgentError,
    AgentObservation,
    EvidenceRef,
)
from data_agent.analysis_agent.state import (
    AgentStatus,
    append_observations,
    append_unique_artifacts,
    append_unique_evidence,
    transition_agent_status,
)
from data_agent.runtime.errors import ErrorCode


def observation(summary: str = "done") -> AgentObservation:
    return AgentObservation(
        observation_id="observation-1",
        action_id="action-1",
        tool_name="catalog.inspect",
        status="succeeded",
        summary=summary,
    )


def artifact(digest: str = "b" * 64) -> AgentArtifactRef:
    return AgentArtifactRef(
        artifact_id="artifact-1",
        run_id="run-1",
        kind="catalog",
        digest=digest,
        sensitivity="metadata",
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
    )


def evidence(digest: str = "b" * 64) -> EvidenceRef:
    return EvidenceRef(
        evidence_id="evidence-1",
        claim_key="catalog",
        artifact_id="artifact-1",
        source_id="orders",
        source_version=1,
        binding_id="binding-1",
        binding_version=1,
        schema_fingerprint="a" * 64,
        result_digest=digest,
        field_refs=("orders.id",),
    )


@pytest.mark.parametrize(
    ("reducer", "item"),
    (
        (append_observations, observation()),
        (append_unique_artifacts, artifact()),
        (append_unique_evidence, evidence()),
    ),
)
def test_append_reducers_preserve_order_and_are_replay_idempotent(reducer, item) -> None:
    first = reducer([], [item])
    replayed = reducer(first, [item])
    assert first == replayed == [item]
    assert first is not replayed


def test_append_reducers_reject_conflicting_ids_without_mutating_input() -> None:
    current_observations = [observation()]
    with pytest.raises(ValueError, match="conflicting"):
        append_observations(current_observations, [observation("different")])
    assert current_observations == [observation()]

    current_artifacts = [artifact()]
    with pytest.raises(ValueError, match="conflicting"):
        append_unique_artifacts(current_artifacts, [artifact("c" * 64)])
    assert current_artifacts == [artifact()]

    current_evidence = [evidence()]
    with pytest.raises(ValueError, match="conflicting"):
        append_unique_evidence(current_evidence, [evidence("c" * 64)])
    assert current_evidence == [evidence()]


def test_reducers_revalidate_values() -> None:
    with pytest.raises(Exception):
        append_observations([], [{"observation_id": "bad"}])


@pytest.mark.parametrize(
    ("current", "target"),
    (
        (AgentStatus.INITIALIZING, AgentStatus.RUNNING),
        (AgentStatus.INITIALIZING, AgentStatus.FAILED),
        (AgentStatus.RUNNING, AgentStatus.WAITING_INPUT),
        (AgentStatus.RUNNING, AgentStatus.COMPLETED),
        (AgentStatus.WAITING_INPUT, AgentStatus.RUNNING),
        (AgentStatus.WAITING_INPUT, AgentStatus.CANCELLED),
        (AgentStatus.COMPLETED, AgentStatus.COMPLETED),
    ),
)
def test_valid_status_transitions(current: AgentStatus, target: AgentStatus) -> None:
    assert transition_agent_status(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    (
        (AgentStatus.INITIALIZING, AgentStatus.COMPLETED),
        (AgentStatus.WAITING_INPUT, AgentStatus.COMPLETED),
        (AgentStatus.COMPLETED, AgentStatus.RUNNING),
        (AgentStatus.FAILED, AgentStatus.RUNNING),
        (AgentStatus.CANCELLED, AgentStatus.RUNNING),
    ),
)
def test_invalid_status_transitions_are_rejected(
    current: AgentStatus,
    target: AgentStatus,
) -> None:
    with pytest.raises(ValueError, match="invalid agent status transition"):
        transition_agent_status(current, target)


def test_failed_observation_can_only_contain_safe_public_error() -> None:
    failed = AgentObservation(
        observation_id="observation-1",
        action_id="action-1",
        tool_name="catalog.inspect",
        status="failed",
        summary="Catalog inspection failed",
        error=AgentError(
            code=ErrorCode.AGENT_ARTIFACT_NOT_FOUND,
            message="Artifact not found",
        ),
    )
    assert append_observations([], [failed]) == [failed]
