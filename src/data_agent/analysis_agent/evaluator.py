"""Deterministic-first progress evaluation for analysis runs."""

from __future__ import annotations

import math
from collections.abc import Sequence

from .models import (
    AgentArtifactKind,
    AgentArtifactRef,
    AgentObservation,
    AnalysisPlan,
    DatasetAuthority,
    EvaluationDecision,
    EvidenceRef,
)
from .prompts import (
    EVALUATOR_SYSTEM_PROMPT,
    ModelClient,
    build_evaluator_prompt,
    complete_strict_model,
)


class AnalysisEvaluator:
    def __init__(self, model_client: ModelClient, *, max_attempts: int = 2) -> None:
        if not 1 <= max_attempts <= 3:
            raise ValueError("evaluator max_attempts must be between 1 and 3")
        self._model_client = model_client
        self._max_attempts = max_attempts

    async def evaluate(
        self,
        *,
        run_id: str,
        plan: AnalysisPlan,
        authority: DatasetAuthority,
        observations: Sequence[AgentObservation],
        artifacts: Sequence[AgentArtifactRef],
        evidence: Sequence[EvidenceRef],
        required_evidence_keys: Sequence[str] = (),
        deterministic_contradictions: Sequence[str] = (),
        budget_exhausted: bool = False,
        max_observation_cells: int = 400,
    ) -> EvaluationDecision:
        required_keys = tuple(dict.fromkeys(required_evidence_keys))
        completed = tuple(
            step.step_id for step in plan.steps if step.status == "completed"
        )
        deterministic = self._pre_model_decision(
            plan=plan,
            authority=authority,
            run_id=run_id,
            observations=observations,
            artifacts=artifacts,
            evidence=evidence,
            required_evidence_keys=required_keys,
            deterministic_contradictions=deterministic_contradictions,
            budget_exhausted=budget_exhausted,
            completed_step_ids=completed,
            required_step_ids=tuple(
                step.step_id for step in plan.steps if step.status != "skipped"
            ),
        )
        if deterministic is not None:
            return deterministic

        evidence_by_claim = {item.claim_key for item in evidence}
        missing = tuple(
            key for key in required_keys if key not in evidence_by_claim
        )
        checks = {
            "toolFailure": False,
            "emptyResult": False,
            "schemaMismatch": False,
            "artifactDigestMismatch": False,
            "nonFiniteResult": False,
            "budgetExhausted": False,
            "missingRequiredEvidence": list(missing),
        }
        prompt = build_evaluator_prompt(
            plan=plan,
            observations=observations,
            evidence=evidence,
            required_evidence_keys=required_keys,
            deterministic_checks=checks,
            output_schema=EvaluationDecision,
            max_observation_cells=max_observation_cells,
        )

        def validate(decision: EvaluationDecision) -> None:
            known_steps = {step.step_id for step in plan.steps}
            unknown = set(decision.completed_step_ids) - known_steps
            if unknown:
                raise ValueError("evaluation completed unknown plan steps")
            if decision.evidence_sufficient:
                if not evidence or missing or decision.missing_evidence:
                    raise ValueError("evaluation cannot claim sufficient evidence")
                if decision.contradictions:
                    raise ValueError("contradictory evidence cannot be sufficient")
            if missing and not set(missing).issubset(set(decision.missing_evidence)):
                raise ValueError("evaluation omitted deterministically missing evidence")
            if decision.decision == "finish":
                required_steps = {
                    step.step_id for step in plan.steps if step.status != "skipped"
                }
                if not required_steps.issubset(set(decision.completed_step_ids)):
                    raise ValueError("finish requires every non-skipped plan step")

        return await complete_strict_model(
            model_client=self._model_client,
            prompt=prompt,
            system=EVALUATOR_SYSTEM_PROMPT,
            output_type=EvaluationDecision,
            task="evaluation_decision",
            max_attempts=self._max_attempts,
            validator=validate,
        )

    def requires_model_call(
        self,
        *,
        run_id: str,
        plan: AnalysisPlan,
        authority: DatasetAuthority,
        observations: Sequence[AgentObservation],
        artifacts: Sequence[AgentArtifactRef],
        evidence: Sequence[EvidenceRef],
        required_evidence_keys: Sequence[str] = (),
        deterministic_contradictions: Sequence[str] = (),
        budget_exhausted: bool = False,
        **_: object,
    ) -> bool:
        required_keys = tuple(dict.fromkeys(required_evidence_keys))
        completed = tuple(
            step.step_id for step in plan.steps if step.status == "completed"
        )
        return self._pre_model_decision(
            plan=plan,
            authority=authority,
            run_id=run_id,
            observations=observations,
            artifacts=artifacts,
            evidence=evidence,
            required_evidence_keys=required_keys,
            deterministic_contradictions=deterministic_contradictions,
            budget_exhausted=budget_exhausted,
            completed_step_ids=completed,
            required_step_ids=tuple(
                step.step_id for step in plan.steps if step.status != "skipped"
            ),
        ) is None

    @classmethod
    def _pre_model_decision(
        cls,
        *,
        plan: AnalysisPlan,
        authority: DatasetAuthority,
        run_id: str,
        observations: Sequence[AgentObservation],
        artifacts: Sequence[AgentArtifactRef],
        evidence: Sequence[EvidenceRef],
        required_evidence_keys: Sequence[str],
        deterministic_contradictions: Sequence[str],
        budget_exhausted: bool,
        completed_step_ids: tuple[str, ...],
        required_step_ids: tuple[str, ...],
    ) -> EvaluationDecision | None:
        deterministic = cls._deterministic_decision(
            authority=authority,
            run_id=run_id,
            observations=observations,
            artifacts=artifacts,
            evidence=evidence,
            required_evidence_keys=required_evidence_keys,
            deterministic_contradictions=deterministic_contradictions,
            budget_exhausted=budget_exhausted,
            completed_step_ids=completed_step_ids,
            required_step_ids=required_step_ids,
        )
        if deterministic is not None:
            return deterministic
        incomplete = any(
            step.status not in {"completed", "skipped"}
            for step in plan.steps
        )
        if observations and incomplete:
            return EvaluationDecision(
                decision="continue",
                evidence_sufficient=False,
                completed_step_ids=completed_step_ids,
                missing_evidence=tuple(required_evidence_keys),
                contradictions=(),
                rationale_summary=(
                    "The latest governed tool succeeded and the finite plan still "
                    "has executable steps."
                ),
            )
        if observations and not evidence:
            return EvaluationDecision(
                decision="continue",
                evidence_sufficient=False,
                completed_step_ids=completed_step_ids,
                missing_evidence=tuple(required_evidence_keys),
                contradictions=(),
                rationale_summary="The governed result still requires evidence binding.",
            )
        return None

    @staticmethod
    def _deterministic_decision(
        *,
        authority: DatasetAuthority,
        run_id: str,
        observations: Sequence[AgentObservation],
        artifacts: Sequence[AgentArtifactRef],
        evidence: Sequence[EvidenceRef],
        required_evidence_keys: Sequence[str],
        deterministic_contradictions: Sequence[str],
        budget_exhausted: bool,
        completed_step_ids: tuple[str, ...],
        required_step_ids: tuple[str, ...],
    ) -> EvaluationDecision | None:
        evidence_claims = {item.claim_key for item in evidence}
        missing = tuple(
            key for key in required_evidence_keys if key not in evidence_claims
        )
        if budget_exhausted:
            return EvaluationDecision(
                decision="fail",
                evidence_sufficient=False,
                completed_step_ids=completed_step_ids,
                missing_evidence=missing or ("remaining execution budget",),
                contradictions=(),
                rationale_summary="The finite analysis budget is exhausted.",
            )
        if observations and observations[-1].status == "failed":
            error = observations[-1].error
            assert error is not None
            return EvaluationDecision(
                decision="replan" if error.retryable else "fail",
                evidence_sufficient=False,
                completed_step_ids=completed_step_ids,
                missing_evidence=missing or ("successful tool result",),
                contradictions=(f"tool_error:{error.code.value}",),
                rationale_summary=(
                    "The latest tool failed; deterministic error handling takes precedence."
                ),
            )

        artifacts_by_id = {item.artifact_id: item for item in artifacts}
        for artifact in artifacts:
            if (
                artifact.run_id != run_id
                or (
                    artifact.schema_digest is not None
                    and artifact.schema_digest != authority.schema_fingerprint
                )
            ):
                return EvaluationDecision(
                    decision="fail",
                    evidence_sufficient=False,
                    completed_step_ids=completed_step_ids,
                    missing_evidence=missing,
                    contradictions=("artifact_run_or_schema_mismatch",),
                    rationale_summary="An artifact does not match the current run and dataset schema.",
                )
        for item in evidence:
            artifact = artifacts_by_id.get(item.artifact_id)
            if (
                item.source_id != authority.source_id
                or item.source_version != authority.source_version
                or item.binding_id != authority.binding_id
                or item.binding_version != authority.binding_version
                or item.schema_fingerprint != authority.schema_fingerprint
                or artifact is None
                or artifact.digest != item.result_digest
            ):
                return EvaluationDecision(
                    decision="fail",
                    evidence_sufficient=False,
                    completed_step_ids=completed_step_ids,
                    missing_evidence=missing,
                    contradictions=("evidence_authority_or_digest_mismatch",),
                    rationale_summary="Evidence failed deterministic authority validation.",
                )
        if deterministic_contradictions:
            return EvaluationDecision(
                decision="replan",
                evidence_sufficient=False,
                completed_step_ids=completed_step_ids,
                missing_evidence=missing,
                contradictions=tuple(dict.fromkeys(deterministic_contradictions)),
                rationale_summary="Deterministic result checks found a contradiction.",
            )
        latest_artifacts = observations[-1].artifact_refs if observations else ()
        if any(
            item.kind in {AgentArtifactKind.QUERY_PREVIEW, AgentArtifactKind.QUERY_RESULT}
            and item.row_count == 0
            for item in latest_artifacts
        ):
            return EvaluationDecision(
                decision="replan",
                evidence_sufficient=False,
                completed_step_ids=completed_step_ids,
                missing_evidence=missing or ("non-empty query result",),
                contradictions=(),
                rationale_summary="The latest governed query returned no rows.",
            )
        if observations and _contains_non_finite(observations[-1].safe_preview):
            return EvaluationDecision(
                decision="replan",
                evidence_sufficient=False,
                completed_step_ids=completed_step_ids,
                missing_evidence=missing or ("finite numeric result",),
                contradictions=("non_finite_result",),
                rationale_summary="The latest result contains a non-finite number.",
            )
        if (
            evidence
            and not missing
            and set(required_step_ids).issubset(set(completed_step_ids))
        ):
            return EvaluationDecision(
                decision="finish",
                evidence_sufficient=True,
                completed_step_ids=completed_step_ids,
                missing_evidence=(),
                contradictions=(),
                rationale_summary=(
                    "All finite plan steps completed with authority-matched evidence."
                ),
            )
        return None


def _contains_non_finite(value: object) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_non_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite(item) for item in value)
    return False


__all__ = ["AnalysisEvaluator"]
