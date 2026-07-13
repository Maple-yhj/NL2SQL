"""Deterministic in-memory profiling of verified query evidence."""

from __future__ import annotations

from ..models import ProviderContext, RetryPolicy, ToolSpec
from .contracts import (
    ColumnProfile,
    ResultProfileInput,
    ResultProfileOutput,
)
from .evidence import EvidenceSigner


RESULT_PROFILE_SPEC = ToolSpec(
    name="result.profile",
    version="1.0.0",
    description="Compute bounded null, distinct, and range statistics for verified rows.",
    input_schema=ResultProfileInput,
    output_schema=ResultProfileOutput,
    risk_level="low",
    side_effects="none",
    required_capabilities=("result.profile",),
    idempotency="safe",
    timeout_seconds=2,
    retry_policy=RetryPolicy(max_attempts=1),
    eval_tags=("profile", "offline"),
)


def _evidence_matches_context(
    logical_plan_hash: str,
    query_hash: str,
    policy_decision_id: str,
    context: ProviderContext,
    data,
    signer: EvidenceSigner,
) -> bool:
    grant = context.access_grant
    return signer.verify(data) and (
        grant.logical_plan_hash == logical_plan_hash
        and grant.prepared_query_hash == query_hash
        and grant.policy_decision_id == policy_decision_id
    )


class ResultProfileProvider:
    spec = RESULT_PROFILE_SPEC

    def __init__(self, evidence_signer: EvidenceSigner) -> None:
        self._evidence_signer = evidence_signer

    async def invoke(
        self,
        payload: ResultProfileInput,
        context: ProviderContext,
    ) -> ResultProfileOutput:
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
        columns: list[ColumnProfile] = []
        warnings: list[str] = []
        for index, name in enumerate(data.columns):
            values = [row.values[index] for row in data.rows]
            non_null = [item for item in values if item is not None]
            distinct = len({(type(item).__name__, repr(item)) for item in non_null})
            minimum = None
            maximum = None
            if non_null:
                try:
                    minimum = min(non_null)
                    maximum = max(non_null)
                except TypeError:
                    warnings.append(f"mixed_types:{name}")
            elif data.rows:
                warnings.append(f"all_null:{name}")
            columns.append(
                ColumnProfile(
                    name=name,
                    null_count=len(values) - len(non_null),
                    distinct_count=distinct,
                    min_value=minimum,
                    max_value=maximum,
                )
            )
        return ResultProfileOutput(
            logical_plan_hash=data.logical_plan_hash,
            query_hash=data.query_hash,
            policy_decision_id=data.policy_decision_id,
            row_count=len(data.rows),
            columns=tuple(columns),
            warnings=tuple(warnings),
        )
