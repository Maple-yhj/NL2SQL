"""Evidence-only answer synthesis and deterministic grounding validation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

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
_GOVERNED_BUSINESS_METRIC = re.compile(
    r"(?:总收入|企业收入|会计收入|净收入|营收|利润|毛利|净利|佣金收入)"
    r"|\b(?:revenue|income|profit|margin|commission revenue)\b",
    re.IGNORECASE,
)


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
        unsupported_metric = _undefined_governed_metric_answer(
            goal=goal,
            observations=observations,
            evidence=evidence,
        )
        if unsupported_metric is not None:
            if mode == AgentMode.PREVIEW:
                unsupported_metric = unsupported_metric.model_copy(
                    update={
                        "limitations": (
                            *unsupported_metric.limitations,
                            _PREVIEW_LIMITATION,
                        )
                    }
                )
            return unsupported_metric
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
            if set(draft.evidence_ids) != allowed_evidence:
                raise ValueError("answer must cite every collected result evidence item")
            for finding in draft.key_findings:
                if set(finding.evidence_ids) - allowed_evidence:
                    raise ValueError("finding references evidence outside the current run")
            if _NUMERIC_TEXT.search(draft.answer) and not draft.evidence_ids:
                raise ValueError("numerical answer text requires evidence")
            if draft.recommended_chart_artifact_id is not None:
                chart = artifacts_by_id.get(draft.recommended_chart_artifact_id)
                if chart is None or chart.kind != AgentArtifactKind.CHART:
                    raise ValueError("recommended chart must be a current-run chart artifact")
            validate_answer_values(
                draft=draft,
                observations=observations,
                artifacts=artifacts,
                evidence=evidence,
                contextual_values=(
                    goal.original_question,
                    goal.contextualized_question,
                    goal.requested_output,
                    goal.success_criteria,
                    goal.constraints,
                ),
            )

        try:
            draft = await complete_strict_model(
                model_client=self._model_client,
                prompt=prompt,
                system=SYNTHESIZER_SYSTEM_PROMPT,
                output_type=AgentAnswerDraft,
                task="answer_draft",
                max_attempts=self._max_attempts,
                validator=validate,
            )
        except ValueError:
            draft = deterministic_evidence_answer(
                observations=observations,
                artifacts=artifacts,
                evidence=evidence,
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


_VALUE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?"
)


def _numeric_tokens(value: object) -> tuple[Decimal, ...]:
    if isinstance(value, bool) or value is None:
        return ()
    if isinstance(value, (int, float, Decimal)):
        try:
            return (Decimal(str(value)),)
        except InvalidOperation:
            return ()
    if isinstance(value, str):
        output: list[Decimal] = []
        for match in _VALUE_TOKEN.finditer(value):
            token = match.group(0)
            percentage = token.endswith("%")
            try:
                number = Decimal(token.rstrip("%").replace(",", ""))
            except InvalidOperation:
                continue
            output.append(number / 100 if percentage else number)
        return tuple(output)
    if isinstance(value, dict):
        return tuple(
            number for nested in value.values() for number in _numeric_tokens(nested)
        )
    if isinstance(value, (list, tuple)):
        return tuple(number for nested in value for number in _numeric_tokens(nested))
    return ()


def _supported_number(number: Decimal, candidates: Sequence[Decimal]) -> bool:
    return any(
        abs(number - candidate)
        <= Decimal("0.000000001") * max(Decimal(1), abs(candidate))
        for candidate in candidates
    )


def _uses_two_decimal_display(field_refs: Sequence[str]) -> bool:
    return any(
        re.search(
            r"(?:^|[._])(?:amount|price|gmv|revenue|sales|cost|fee)$",
            ref,
            flags=re.IGNORECASE,
        )
        for ref in field_refs
    )


def validate_answer_values(
    *,
    draft: AgentAnswerDraft,
    observations: Sequence[AgentObservation],
    artifacts: Sequence[AgentArtifactRef],
    evidence: Sequence[EvidenceRef],
    contextual_values: Sequence[object] = (),
) -> None:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    values_by_artifact: dict[str, list[Decimal]] = {
        item.artifact_id: list(_numeric_tokens(item.row_count)) for item in artifacts
    }
    for observation in observations:
        values = _numeric_tokens(observation.safe_preview)
        for artifact in observation.artifact_refs:
            values_by_artifact.setdefault(artifact.artifact_id, []).extend(values)
    contextual_numbers = _numeric_tokens(contextual_values)

    def candidates(evidence_ids: Sequence[str]) -> tuple[Decimal, ...]:
        output = list(contextual_numbers)
        for evidence_id in evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if item is None:
                continue
            values = values_by_artifact.get(item.artifact_id, ())
            output.extend(values)
            if _uses_two_decimal_display(item.field_refs):
                output.extend(number.quantize(Decimal("0.01")) for number in values)
        return tuple(output)

    answer_values = _numeric_tokens(draft.answer)
    answer_candidates = candidates(draft.evidence_ids)
    if any(not _supported_number(item, answer_candidates) for item in answer_values):
        raise ValueError("answer contains a numeric value absent from cited evidence")
    for finding in draft.key_findings:
        finding_values = _numeric_tokens(finding.claim)
        finding_candidates = candidates(finding.evidence_ids)
        if any(
            not _supported_number(item, finding_candidates)
            for item in finding_values
        ):
            raise ValueError("finding contains a numeric value absent from cited evidence")


def deterministic_evidence_answer(
    *,
    observations: Sequence[AgentObservation],
    artifacts: Sequence[AgentArtifactRef],
    evidence: Sequence[EvidenceRef],
) -> AgentAnswerDraft:
    evidence_ids = tuple(item.evidence_id for item in evidence)
    evidence_artifacts = {item.artifact_id for item in evidence}
    if evidence and all(item.claim_key == "semantic_definition" for item in evidence):
        semantic_row = next(
            (
                observation.safe_preview[0]
                for observation in reversed(observations)
                if observation.tool_name == "semantic.inspect"
                and observation.safe_preview
                and any(
                    artifact.artifact_id in evidence_artifacts
                    for artifact in observation.artifact_refs
                )
            ),
            None,
        )
        if semantic_row is not None:
            fields = semantic_row.get("fields")
            field_items = (
                tuple(item for item in fields if isinstance(item, dict))
                if isinstance(fields, (list, tuple))
                else ()
            )
            lifecycle = tuple(
                (
                    str(item.get("displayName") or item.get("logicalRef") or "field"),
                    str(item["lifecycleStage"]),
                )
                for item in field_items
                if item.get("lifecycleStage")
            )
            if lifecycle:
                summary = "；".join(
                    f"{name}：{stage}" for name, stage in lifecycle[:8]
                )
                answer = f"已验证当前语义绑定中的字段生命周期标注：{summary}。"
                limitation = "仅依据显式 lifecycleStage 元数据分类，未从字段名推断可用时点。"
            else:
                answer = (
                    "当前语义绑定已经验证，但字段没有提供 lifecycleStage 生命周期标注，"
                    "因此无法可靠区分预测时可用字段与数据泄漏字段。请为候选字段补充其"
                    "产生时点和最早可用时点后再分类。"
                )
                limitation = "未从字段名或时间戳名称猜测字段在预测时点是否可用。"
            return AgentAnswerDraft(
                answer=answer,
                key_findings=(
                    {
                        "finding_id": "deterministic_semantic_result",
                        "claim": answer,
                        "evidence_ids": evidence_ids,
                    },
                ),
                recommended_chart_artifact_id=None,
                limitations=(limitation,),
                evidence_ids=evidence_ids,
            )
    row = next(
        (
            observation.safe_preview[0]
            for observation in reversed(observations)
            if observation.safe_preview
            and any(
                artifact.artifact_id in evidence_artifacts
                for artifact in observation.artifact_refs
            )
        ),
        None,
    )
    if row:
        rendered = "，".join(
            f"{key}={_display_value(key, value)}"
            for key, value in list(row.items())[:6]
        )
        answer = f"根据经验证的查询结果：{rendered}。"
        claim = answer
    else:
        row_count = next(
            (
                item.row_count
                for item in artifacts
                if item.artifact_id in evidence_artifacts and item.row_count is not None
            ),
            None,
        )
        answer = (
            f"查询已完成并返回 {row_count} 行经验证结果。"
            if row_count is not None
            else "已取得并验证当前数据集的受治理证据。"
        )
        claim = answer
    return AgentAnswerDraft(
        answer=answer,
        key_findings=(
            {
                "finding_id": "deterministic_evidence_result",
                "claim": claim,
                "evidence_ids": evidence_ids,
            },
        ),
        recommended_chart_artifact_id=None,
        limitations=("答案由经验证证据确定性生成，未补充证据之外的推断。",),
        evidence_ids=evidence_ids,
    )


def _display_value(field_ref: str, value: object) -> object:
    if (
        isinstance(value, float)
        and _uses_two_decimal_display((field_ref,))
    ):
        return f"{value:.2f}"
    return value


def _undefined_governed_metric_answer(
    *,
    goal: AnalysisGoal,
    observations: Sequence[AgentObservation],
    evidence: Sequence[EvidenceRef],
) -> AgentAnswerDraft | None:
    # Scope the safety policy to the current user turn. A conversation summary
    # may mention a previously discussed metric and must not contaminate a new,
    # unrelated semantic question.
    question = goal.original_question
    if not _GOVERNED_BUSINESS_METRIC.search(question):
        return None
    semantic_rows = tuple(
        row
        for observation in observations
        if observation.status == "succeeded"
        and observation.tool_name == "semantic.inspect"
        for row in observation.safe_preview
    )
    if not semantic_rows:
        return None
    metrics = tuple(
        item
        for row in semantic_rows
        for item in (row.get("metrics") or ())
        if isinstance(item, dict)
    )
    lowered = question.casefold()
    for metric in metrics:
        candidates = (
            metric.get("metricRef"),
            metric.get("metric_ref"),
            metric.get("displayName"),
            metric.get("display_name"),
            *((metric.get("synonyms") or ()) if isinstance(metric.get("synonyms"), (list, tuple)) else ()),
        )
        if any(
            isinstance(item, str) and item.casefold() in lowered
            for item in candidates
        ):
            return None
    evidence_ids = tuple(item.evidence_id for item in evidence)
    if re.search(r"[\u3400-\u9fff]", question):
        answer = (
            "当前数据集没有与该业务概念匹配的受治理指标定义，因此不能把某个相近字段直接当作该指标。"
            "需要先明确来源字段、聚合方式、单位、粒度以及业务或会计范围。"
        )
        limitation = "未使用字段名猜测业务或会计口径。"
    else:
        answer = (
            "This dataset has no governed metric definition matching the requested "
            "business concept, so a similarly named field cannot be substituted. "
            "Define the source field, aggregation, unit, grain, and business or "
            "accounting scope first."
        )
        limitation = "No business or accounting meaning was inferred from field names."
    return AgentAnswerDraft(
        answer=answer,
        key_findings=(
            {
                "finding_id": "undefined_governed_business_metric",
                "claim": answer,
                "evidence_ids": evidence_ids,
            },
        ),
        recommended_chart_artifact_id=None,
        limitations=(limitation,),
        evidence_ids=evidence_ids,
    )
__all__ = [
    "AnalysisSynthesizer",
    "deterministic_evidence_answer",
    "validate_answer_values",
]
