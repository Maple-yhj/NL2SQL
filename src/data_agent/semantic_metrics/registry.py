"""Persistence protocol for the semantic metric governance control plane."""

from __future__ import annotations

from typing import Protocol

from .models import (
    ActiveMetricSetPointer,
    ConversationMetricPin,
    DomainPackAssignment,
    MetricOverlay,
    MetricProposal,
    MetricSetRecord,
    MetricValidationReport,
    SemanticAuditEvent,
)


class SemanticMetricRegistry(Protocol):
    async def save_metric_proposal(self, proposal: MetricProposal) -> MetricProposal: ...

    async def update_metric_proposal(
        self,
        proposal: MetricProposal,
        *,
        expected_revision: int,
    ) -> MetricProposal: ...

    async def get_metric_proposal(
        self,
        *,
        tenant_id: str,
        proposal_id: str,
    ) -> MetricProposal | None: ...

    async def list_metric_proposals(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[MetricProposal, ...]: ...

    async def save_metric_validation_report(
        self,
        report: MetricValidationReport,
    ) -> MetricValidationReport: ...

    async def get_metric_validation_report(
        self,
        *,
        tenant_id: str,
        report_id: str,
    ) -> MetricValidationReport | None: ...

    async def save_metric_set(self, metric_set: MetricSetRecord) -> MetricSetRecord: ...

    async def get_metric_set(
        self,
        *,
        tenant_id: str,
        metric_set_id: str,
        version: int,
    ) -> MetricSetRecord | None: ...

    async def list_metric_sets(
        self,
        *,
        tenant_id: str,
        source_id: str,
        domain_id: str,
    ) -> tuple[MetricSetRecord, ...]: ...

    async def activate_metric_set(
        self,
        *,
        tenant_id: str,
        metric_set_id: str,
        version: int,
        expected_pointer_revision: int,
    ) -> ActiveMetricSetPointer: ...

    async def get_active_metric_set(
        self,
        *,
        tenant_id: str,
        source_id: str,
        domain_id: str,
    ) -> ActiveMetricSetPointer | None: ...

    async def save_metric_overlay(self, overlay: MetricOverlay) -> MetricOverlay: ...

    async def get_metric_overlay(
        self,
        *,
        tenant_id: str,
        overlay_id: str,
    ) -> MetricOverlay | None: ...

    async def put_conversation_metric_pin(
        self,
        pin: ConversationMetricPin,
        *,
        expected_revision: int,
    ) -> ConversationMetricPin: ...

    async def get_conversation_metric_pin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationMetricPin | None: ...

    async def put_domain_pack_assignment(
        self,
        assignment: DomainPackAssignment,
        *,
        expected_revision: int,
    ) -> DomainPackAssignment: ...

    async def get_domain_pack_assignment(
        self,
        *,
        tenant_id: str,
        source_id: str,
        domain_id: str,
    ) -> DomainPackAssignment | None: ...

    async def append_semantic_audit_event(
        self,
        event: SemanticAuditEvent,
    ) -> SemanticAuditEvent: ...

    async def list_semantic_audit_events(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
    ) -> tuple[SemanticAuditEvent, ...]: ...


__all__ = ["SemanticMetricRegistry"]
