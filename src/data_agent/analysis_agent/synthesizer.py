"""Evidence-only answer synthesis and deterministic grounding validation."""

from __future__ import annotations

import re
from collections.abc import Sequence

from data_agent.runtime.models import AgentMode

from .models import (
    AgentAnswerDraft,
    AgentArtifactKind,
    AgentArtifactRef,
    AgentObservation,
    AnalysisGoal,
    DatasetAuthority,
    EvidenceRef,
)
from .prompts import (
    ModelClient,
    SYNTHESIZER_SYSTEM_PROMPT,
    build_synthesizer_prompt,
    complete_strict_model,
)


_NUMERIC_TEXT = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)*(?:%|\b)")
_PREVIEW_LIMITATION = "结论基于预览数据，完整结果可能不同。"


class AnalysisSynthesizer:
    def __init__(self, model_client: ModelClient, *, max_attempts: int = 2) -> None:
        if not 1 <= max_attempts <= 3:
            raise ValueError("synthesizer max_attempts must be between 1 and 3")
        self._model_client = model_client
        self._max_attempts = max_attempts

    async def synthesize(
        self,
        *,
        run_id: str,
        goal: AnalysisGoal,
        mode: AgentMode,
        authority: DatasetAuthority,
        observations: Sequence[AgentObservation],
        artifacts: Sequence[AgentArtifactRef],
        evidence: Sequence[EvidenceRef],
        max_observation_cells: int = 400,
    ) -> AgentAnswerDraft:
        if not evidence:
            raise ValueError("validated evidence is required before answer synthesis")
        if mode != authority.mode:
            raise ValueError("synthesis mode must match current dataset authority")
        self._validate_evidence_authority(
            run_id=run_id,
            authority=authority,
            artifacts=artifacts,
            evidence=evidence,
        )
        prompt = build_synthesizer_prompt(
            goal=goal,
            mode=mode.value,
            observations=observations,
            evidence=evidence,
            artifacts=artifacts,
            output_schema=AgentAnswerDraft,
            max_observation_cells=max_observation_cells,
        )
        allowed_evidence = {item.evidence_id for item in evidence}
        artifacts_by_id = {item.artifact_id: item for item in artifacts}

        def validate(draft: AgentAnswerDraft) -> None:
            unknown = set(draft.evidence_ids) - allowed_evidence
            if unknown:
                raise ValueError("answer references evidence outside the current run")
            for finding in draft.key_findings:
                if set(finding.evidence_ids) - allowed_evidence:
                    raise ValueError("finding references evidence outside the current run")
            if _NUMERIC_TEXT.search(draft.answer) and not draft.evidence_ids:
                raise ValueError("numerical answer text requires evidence")
            if draft.recommended_chart_artifact_id is not None:
                chart = artifacts_by_id.get(draft.recommended_chart_artifact_id)
                if chart is None or chart.kind != AgentArtifactKind.CHART:
                    raise ValueError("recommended chart must be a current-run chart artifact")

        draft = await complete_strict_model(
            model_client=self._model_client,
            prompt=prompt,
            system=SYNTHESIZER_SYSTEM_PROMPT,
            output_type=AgentAnswerDraft,
            task="answer_draft",
            max_attempts=self._max_attempts,
            validator=validate,
        )
        if mode == AgentMode.PREVIEW and _PREVIEW_LIMITATION not in draft.limitations:
            draft = draft.model_copy(
                update={"limitations": (*draft.limitations, _PREVIEW_LIMITATION)}
            )
        return draft

    @staticmethod
    def _validate_evidence_authority(
        *,
        run_id: str,
        authority: DatasetAuthority,
        artifacts: Sequence[AgentArtifactRef],
        evidence: Sequence[EvidenceRef],
    ) -> None:
        artifacts_by_id = {item.artifact_id: item for item in artifacts}
        if len(artifacts_by_id) != len(artifacts):
            raise ValueError("artifact IDs must be unique before synthesis")
        evidence_ids = {item.evidence_id for item in evidence}
        if len(evidence_ids) != len(evidence):
            raise ValueError("evidence IDs must be unique before synthesis")
        for item in evidence:
            artifact = artifacts_by_id.get(item.artifact_id)
            if (
                artifact is None
                or artifact.run_id != run_id
                or artifact.digest != item.result_digest
            ):
                raise ValueError("evidence does not match a current-run artifact")
            if (
                item.source_id != authority.source_id
                or item.source_version != authority.source_version
                or item.binding_id != authority.binding_id
                or item.binding_version != authority.binding_version
                or item.schema_fingerprint != authority.schema_fingerprint
            ):
                raise ValueError("evidence does not match current dataset authority")


__all__ = ["AnalysisSynthesizer"]
