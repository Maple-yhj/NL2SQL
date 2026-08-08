"""Evidence collection bound to current-run artifacts and immutable pins."""

from __future__ import annotations

import hashlib

from data_agent.analysis_agent.models import EvidenceRef
from data_agent.runtime.models import AgentMode
from data_agent.tools.models import ProviderContext, ToolSpec

from .base import dataset_runtime
from .contracts import EvidenceCollectInput, EvidenceCollectOutput


EVIDENCE_COLLECT_SPEC = ToolSpec(
    name="evidence.collect", version="1.0.0", description="Bind a claim to a verified result artifact",
    input_schema=EvidenceCollectInput, output_schema=EvidenceCollectOutput, risk_level="low",
    side_effects="none", required_capabilities=("evidence.collect",), idempotency="safe",
    timeout_seconds=10, authority_kinds=("dataset",),
    allowed_modes=(AgentMode.PLAN, AgentMode.PREVIEW, AgentMode.EXECUTE), artifact_policy="metadata",
    credential_requirement="none",
)


class EvidenceCollectProvider:
    spec = EVIDENCE_COLLECT_SPEC

    async def invoke(self, payload: EvidenceCollectInput, context: ProviderContext) -> EvidenceCollectOutput:
        runtime = dataset_runtime(context)
        refs = await runtime.artifacts.list_for_run(
            tenant_id=runtime.authority.tenant_id,
            user_id=runtime.authority.user_id,
            run_id=context.run_id,
        )
        artifact = next((item for item in refs if item.artifact_id == payload.artifact_id), None)
        if artifact is None:
            await runtime.artifacts.get_json(
                tenant_id=runtime.authority.tenant_id,
                user_id=runtime.authority.user_id,
                run_id=context.run_id,
                artifact_id=payload.artifact_id,
            )
            raise AssertionError("unreachable")
        claim_digest = hashlib.sha256(payload.claim_key.encode("utf-8")).hexdigest()
        evidence_id = "evidence-" + artifact.digest[:32] + "-" + claim_digest[:32]
        evidence = EvidenceRef(
            evidence_id=evidence_id,
            claim_key=payload.claim_key,
            artifact_id=artifact.artifact_id,
            source_id=runtime.authority.source_id,
            source_version=runtime.authority.source_version,
            binding_id=runtime.authority.binding_id,
            binding_version=runtime.authority.binding_version,
            schema_fingerprint=runtime.authority.schema_fingerprint,
            sql_digest=payload.sql_digest,
            result_digest=artifact.digest,
            field_refs=payload.field_refs,
        )
        return EvidenceCollectOutput(
            summary=f"Collected evidence for {payload.claim_key}",
            artifact=artifact,
            evidence=evidence,
        )


__all__ = ["EVIDENCE_COLLECT_SPEC", "EvidenceCollectProvider"]
