"""Deterministic rendering over policy-verified result evidence."""

from __future__ import annotations

from datetime import date, datetime

from ..models import ProviderContext, RetryPolicy, ToolSpec
from .contracts import AnswerRenderInput, AnswerRenderOutput
from .evidence import EvidenceSigner
from .profile import _evidence_matches_context


ANSWER_RENDER_SPEC = ToolSpec(
    name="answer.render",
    version="1.0.0",
    description="Render a deterministic answer and table from verified query evidence.",
    input_schema=AnswerRenderInput,
    output_schema=AnswerRenderOutput,
    risk_level="low",
    side_effects="none",
    required_capabilities=("answer.render",),
    idempotency="safe",
    timeout_seconds=2,
    retry_policy=RetryPolicy(max_attempts=1),
    eval_tags=("answer", "verified-evidence", "offline"),
)


def _format_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        text = value.isoformat()
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


class AnswerRenderProvider:
    spec = ANSWER_RENDER_SPEC

    def __init__(self, evidence_signer: EvidenceSigner) -> None:
        self._evidence_signer = evidence_signer

    async def invoke(
        self,
        payload: AnswerRenderInput,
        context: ProviderContext,
    ) -> AnswerRenderOutput:
        data = payload.data
        if not _evidence_matches_context(
            data.logical_plan_hash,
            data.query_hash,
            data.policy_decision_id,
            context,
            data,
            self._evidence_signer,
        ):
            raise PermissionError("query evidence is not verified for this invocation")
        profile = payload.profile
        if profile is not None and (
            profile.logical_plan_hash != data.logical_plan_hash
            or profile.query_hash != data.query_hash
            or profile.policy_decision_id != data.policy_decision_id
            or profile.row_count != len(data.rows)
        ):
            raise PermissionError("result profile does not match query evidence")
        if data.rows:
            answer = f"已基于验证后的查询证据返回 {len(data.rows)} 行结果。"
        else:
            answer = "验证后的查询未返回结果。"
        if data.rows:
            first_row = ", ".join(
                f"{column}={_format_cell(value)}"
                for column, value in zip(
                    data.columns,
                    data.rows[0].values,
                    strict=True,
                )
            )
            answer = f"{answer} First verified row: {first_row}."
        table = ""
        if data.columns:
            header = "| " + " | ".join(_format_cell(item) for item in data.columns) + " |"
            divider = "| " + " | ".join("---" for _ in data.columns) + " |"
            body = [
                "| " + " | ".join(_format_cell(item) for item in row.values) + " |"
                for row in data.rows[:50]
            ]
            table = "\n".join((header, divider, *body))
        return AnswerRenderOutput(
            answer=answer,
            table_markdown=table,
            evidence_query_hash=data.query_hash,
            policy_decision_id=data.policy_decision_id,
        )
