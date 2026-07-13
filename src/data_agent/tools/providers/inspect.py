"""Authorized live catalog inspection provider."""

from __future__ import annotations

from ..models import ProviderContext, RetryPolicy, ToolSpec
from .contracts import DataInspectInput, DataInspectOutput


DATA_INSPECT_SPEC = ToolSpec(
    name="data.inspect",
    version="1.0.0",
    description="Inspect an authorized PostgreSQL schema snapshot.",
    input_schema=DataInspectInput,
    output_schema=DataInspectOutput,
    risk_level="medium",
    side_effects="read",
    required_capabilities=("data.inspect",),
    idempotency="safe",
    timeout_seconds=5,
    retry_policy=RetryPolicy(max_attempts=2, initial_backoff_seconds=0.05),
    eval_tags=("catalog", "authorized-read"),
)


class DataInspectProvider:
    spec = DATA_INSPECT_SPEC

    def __init__(self, connector: object) -> None:
        self._connector = connector

    async def invoke(
        self,
        payload: DataInspectInput,
        context: ProviderContext,
    ) -> DataInspectOutput:
        if context.credential is None:
            raise PermissionError("catalog inspection requires a credential lease")
        catalog = await self._connector.introspect_schema(
            context.access_grant,
            context.credential,
            relations=payload.relations,
        )
        return DataInspectOutput(catalog=catalog)
