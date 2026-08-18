"""Tenant-isolated datasource, snapshot, binding, and connector registries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from data_agent.tools.connectors import DataSourceConnector
from data_agent.relationships.models import (
    RelationshipGraphDraft,
    RelationshipRecommendationRun,
)
from data_agent.semantic_metrics.digest import semantic_digest
from data_agent.semantic_metrics.models import (
    ActiveMetricSetPointer,
    ConversationMetricPin,
    DomainPackAssignment,
    MetricOverlay,
    MetricProposal,
    MetricSetRecord,
    MetricSetStatus,
    MetricValidationReport,
    SemanticAuditEvent,
)

from .models import (
    ConversationDataSourcePin,
    DataSourceDefinition,
    DataSourceKind,
    DataSourceSnapshot,
    DataSourceStatus,
    SemanticBindingRecord,
    SemanticBindingStatus,
    SemanticGraphBindingRecord,
)


class DataSourceRegistryErrorCode(StrEnum):
    ALREADY_EXISTS = "ALREADY_EXISTS"
    NOT_FOUND = "NOT_FOUND"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    INVALID_BINDING = "INVALID_BINDING"
    CONNECTOR_MISMATCH = "CONNECTOR_MISMATCH"
    GRAPH_REVISION_CONFLICT = "GRAPH_REVISION_CONFLICT"
    GRAPH_STALE_SNAPSHOT = "GRAPH_STALE_SNAPSHOT"
    METRIC_REVISION_CONFLICT = "METRIC_REVISION_CONFLICT"
    INVALID_METRIC_STATE = "INVALID_METRIC_STATE"
    METRIC_SET_REVOKED = "METRIC_SET_REVOKED"


class DataSourceRegistryError(RuntimeError):
    def __init__(
        self,
        code: DataSourceRegistryErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


class DataSourceRegistry(Protocol):
    """Persistence boundary for datasource control-plane state."""

    async def create(
        self,
        definition: DataSourceDefinition,
    ) -> DataSourceDefinition: ...

    async def get(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> DataSourceDefinition: ...

    async def list(
        self,
        *,
        tenant_id: str,
    ) -> tuple[DataSourceDefinition, ...]: ...

    async def delete(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> DataSourceDefinition: ...

    async def publish_snapshot(
        self,
        snapshot: DataSourceSnapshot,
    ) -> DataSourceDefinition: ...

    async def get_snapshot(
        self,
        *,
        tenant_id: str,
        source_id: str,
        version: int,
    ) -> DataSourceSnapshot: ...

    async def save_binding(
        self,
        binding: SemanticBindingRecord,
    ) -> SemanticBindingRecord: ...

    async def list_bindings(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[SemanticBindingRecord, ...]: ...

    async def activate_binding(
        self,
        *,
        tenant_id: str,
        binding_id: str,
    ) -> SemanticBindingRecord: ...

    async def create_graph_draft(
        self, draft: RelationshipGraphDraft
    ) -> RelationshipGraphDraft: ...

    async def get_graph_draft(
        self, *, tenant_id: str, source_id: str, source_snapshot_version: int
    ) -> RelationshipGraphDraft | None: ...

    async def save_graph_draft(
        self, draft: RelationshipGraphDraft, *, expected_revision: int
    ) -> RelationshipGraphDraft: ...

    async def save_recommendation_run(
        self, run: RelationshipRecommendationRun
    ) -> RelationshipRecommendationRun: ...

    async def get_recommendation_run(
        self, *, tenant_id: str, run_id: str
    ) -> RelationshipRecommendationRun | None: ...

    async def activate_graph_binding(
        self, binding: SemanticGraphBindingRecord
    ) -> SemanticGraphBindingRecord: ...

    async def list_graph_bindings(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[SemanticGraphBindingRecord, ...]: ...

    async def pin_conversation(
        self,
        pin: ConversationDataSourcePin,
    ) -> ConversationDataSourcePin: ...

    async def get_conversation_pin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationDataSourcePin | None: ...


class InMemoryDataSourceRegistry:
    """Reference control-plane implementation with strict tenant isolation."""

    def __init__(self) -> None:
        self._sources: dict[tuple[str, str], DataSourceDefinition] = {}
        self._snapshots: dict[tuple[str, str, int], DataSourceSnapshot] = {}
        self._bindings: dict[tuple[str, str], SemanticBindingRecord] = {}
        self._conversation_pins: dict[
            tuple[str, str, str], ConversationDataSourcePin
        ] = {}
        self._graph_drafts: dict[tuple[str, str], RelationshipGraphDraft] = {}
        self._recommendation_runs: dict[
            tuple[str, str], RelationshipRecommendationRun
        ] = {}
        self._graph_bindings: dict[tuple[str, str], SemanticGraphBindingRecord] = {}
        self._metric_proposals: dict[tuple[str, str], MetricProposal] = {}
        self._metric_validation_reports: dict[
            tuple[str, str], MetricValidationReport
        ] = {}
        self._metric_sets: dict[tuple[str, str, int], MetricSetRecord] = {}
        self._active_metric_sets: dict[
            tuple[str, str, str], ActiveMetricSetPointer
        ] = {}
        self._metric_overlays: dict[tuple[str, str], MetricOverlay] = {}
        self._conversation_metric_pins: dict[
            tuple[str, str, str], ConversationMetricPin
        ] = {}
        self._domain_pack_assignments: dict[
            tuple[str, str, str], DomainPackAssignment
        ] = {}
        self._semantic_audit_events: dict[
            tuple[str, str], SemanticAuditEvent
        ] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        definition: DataSourceDefinition,
    ) -> DataSourceDefinition:
        key = (definition.tenant_id, definition.source_id)
        async with self._lock:
            if key in self._sources:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "datasource already exists",
                )
            self._sources[key] = definition
            return definition

    async def get(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> DataSourceDefinition:
        async with self._lock:
            source = self._sources.get((tenant_id, source_id))
            if source is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "datasource was not found",
                )
            return source

    async def list(
        self,
        *,
        tenant_id: str,
    ) -> tuple[DataSourceDefinition, ...]:
        async with self._lock:
            return tuple(
                source
                for (candidate_tenant, _), source in sorted(
                    self._sources.items()
                )
                if candidate_tenant == tenant_id
            )

    async def delete(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> DataSourceDefinition:
        key = (tenant_id, source_id)
        async with self._lock:
            source = self._sources.get(key)
            if source is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "datasource was not found",
                )
            self._sources.pop(key)
            self._snapshots = {
                candidate_key: snapshot
                for candidate_key, snapshot in self._snapshots.items()
                if candidate_key[:2] != key
            }
            binding_ids = {
                binding.binding_id
                for binding in self._bindings.values()
                if binding.tenant_id == tenant_id
                and binding.source_id == source_id
            }
            self._bindings = {
                candidate_key: binding
                for candidate_key, binding in self._bindings.items()
                if binding.binding_id not in binding_ids
                or binding.tenant_id != tenant_id
            }
            self._conversation_pins = {
                candidate_key: pin
                for candidate_key, pin in self._conversation_pins.items()
                if pin.tenant_id != tenant_id or pin.source_id != source_id
            }
            self._graph_drafts = {
                candidate_key: draft
                for candidate_key, draft in self._graph_drafts.items()
                if draft.tenant_id != tenant_id or draft.source_id != source_id
            }
            self._recommendation_runs = {
                candidate_key: run
                for candidate_key, run in self._recommendation_runs.items()
                if run.tenant_id != tenant_id or run.source_id != source_id
            }
            self._graph_bindings = {
                candidate_key: binding
                for candidate_key, binding in self._graph_bindings.items()
                if binding.tenant_id != tenant_id or binding.source_id != source_id
            }
            self._metric_proposals = {
                candidate_key: proposal
                for candidate_key, proposal in self._metric_proposals.items()
                if proposal.tenant_id != tenant_id or proposal.source_id != source_id
            }
            self._metric_validation_reports = {
                candidate_key: report
                for candidate_key, report in self._metric_validation_reports.items()
                if report.tenant_id != tenant_id or report.source_id != source_id
            }
            self._metric_sets = {
                candidate_key: metric_set
                for candidate_key, metric_set in self._metric_sets.items()
                if metric_set.tenant_id != tenant_id or metric_set.source_id != source_id
            }
            self._active_metric_sets = {
                candidate_key: pointer
                for candidate_key, pointer in self._active_metric_sets.items()
                if pointer.tenant_id != tenant_id or pointer.source_id != source_id
            }
            self._metric_overlays = {
                candidate_key: overlay
                for candidate_key, overlay in self._metric_overlays.items()
                if overlay.tenant_id != tenant_id
                or overlay.base_context.source_id != source_id
            }
            self._conversation_metric_pins = {
                candidate_key: pin
                for candidate_key, pin in self._conversation_metric_pins.items()
                if pin.tenant_id != tenant_id or pin.context.source_id != source_id
            }
            self._domain_pack_assignments = {
                candidate_key: assignment
                for candidate_key, assignment in self._domain_pack_assignments.items()
                if assignment.tenant_id != tenant_id
                or assignment.source_id != source_id
            }
            self._semantic_audit_events = {
                candidate_key: event
                for candidate_key, event in self._semantic_audit_events.items()
                if event.tenant_id != tenant_id or event.source_id != source_id
            }
            return source

    async def publish_snapshot(
        self,
        snapshot: DataSourceSnapshot,
    ) -> DataSourceDefinition:
        source_key = (snapshot.tenant_id, snapshot.source_id)
        snapshot_key = (
            snapshot.tenant_id,
            snapshot.source_id,
            snapshot.version,
        )
        async with self._lock:
            source = self._sources.get(source_key)
            if source is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "datasource was not found",
                )
            expected_version = source.active_snapshot_version + 1
            if snapshot.version != expected_version or snapshot_key in self._snapshots:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.VERSION_CONFLICT,
                    "datasource snapshot version is not the next version",
                )
            self._snapshots[snapshot_key] = snapshot
            updated = source.model_copy(
                update={
                    "revision": source.revision + 1,
                    "active_snapshot_version": snapshot.version,
                    "status": DataSourceStatus.READY,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._sources[source_key] = updated
            return updated

    async def get_snapshot(
        self,
        *,
        tenant_id: str,
        source_id: str,
        version: int,
    ) -> DataSourceSnapshot:
        async with self._lock:
            snapshot = self._snapshots.get((tenant_id, source_id, version))
            if snapshot is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "datasource snapshot was not found",
                )
            return snapshot

    async def save_binding(
        self,
        binding: SemanticBindingRecord,
    ) -> SemanticBindingRecord:
        key = (binding.tenant_id, binding.binding_id)
        async with self._lock:
            if key in self._bindings:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "semantic binding already exists",
                )
            if (
                binding.tenant_id,
                binding.source_id,
                binding.source_snapshot_version,
            ) not in self._snapshots:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "semantic binding source snapshot was not found",
                )
            if binding.status != SemanticBindingStatus.DRAFT:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_BINDING,
                    "new semantic bindings must start as drafts",
                )
            self._bindings[key] = binding
            return binding

    async def list_bindings(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[SemanticBindingRecord, ...]:
        async with self._lock:
            return tuple(
                binding
                for (candidate_tenant, _), binding in sorted(
                    self._bindings.items()
                )
                if candidate_tenant == tenant_id
                and binding.source_id == source_id
            )

    async def activate_binding(
        self,
        *,
        tenant_id: str,
        binding_id: str,
    ) -> SemanticBindingRecord:
        key = (tenant_id, binding_id)
        async with self._lock:
            binding = self._bindings.get(key)
            if binding is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "semantic binding was not found",
                )
            snapshot = self._snapshots.get(
                (
                    tenant_id,
                    binding.source_id,
                    binding.source_snapshot_version,
                )
            )
            if snapshot is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "semantic binding source snapshot was not found",
                )
            catalog = {
                relation.relation: {column.name for column in relation.columns}
                for relation in snapshot.catalog.relations
            }
            invalid = tuple(
                mapping.logical_ref
                for mapping in binding.mappings
                if mapping.physical_relation not in catalog
                or mapping.physical_column
                not in catalog[mapping.physical_relation]
            )
            invalid_relationships = tuple(
                relationship.relationship_id
                for relationship in binding.relationships
                if relationship.left_relation not in catalog
                or relationship.left_column
                not in catalog.get(relationship.left_relation, set())
                or relationship.right_relation not in catalog
                or relationship.right_column
                not in catalog.get(relationship.right_relation, set())
            )
            if invalid or invalid_relationships:
                details = [
                    *invalid,
                    *(f"relationship:{item}" for item in invalid_relationships),
                ]
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_BINDING,
                    "semantic binding references unknown physical fields: "
                    + ", ".join(details),
                )
            now = datetime.now(UTC)
            for candidate_key, candidate in tuple(self._bindings.items()):
                if (
                    candidate_key != key
                    and candidate.tenant_id == tenant_id
                    and candidate.source_id == binding.source_id
                    and candidate.domain_id == binding.domain_id
                    and candidate.status == SemanticBindingStatus.ACTIVE
                ):
                    self._bindings[candidate_key] = candidate.model_copy(
                        update={
                            "status": SemanticBindingStatus.RETIRED,
                            "updated_at": now,
                        }
                    )
            activated = binding.model_copy(
                update={
                    "status": SemanticBindingStatus.ACTIVE,
                    "updated_at": now,
                }
            )
            self._bindings[key] = activated
            return activated

    async def create_graph_draft(
        self, draft: RelationshipGraphDraft
    ) -> RelationshipGraphDraft:
        key = (draft.tenant_id, draft.graph_id)
        async with self._lock:
            if key in self._graph_drafts:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "relationship graph draft already exists",
                )
            if (
                draft.tenant_id,
                draft.source_id,
                draft.source_snapshot_version,
            ) not in self._snapshots:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "relationship graph source snapshot was not found",
                )
            if draft.revision != 1:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.VERSION_CONFLICT,
                    "new relationship graph drafts must start at revision 1",
                )
            self._graph_drafts[key] = draft
            return draft

    async def get_graph_draft(
        self,
        *,
        tenant_id: str,
        source_id: str,
        source_snapshot_version: int,
    ) -> RelationshipGraphDraft | None:
        async with self._lock:
            return next(
                (
                    draft
                    for draft in self._graph_drafts.values()
                    if draft.tenant_id == tenant_id
                    and draft.source_id == source_id
                    and draft.source_snapshot_version == source_snapshot_version
                ),
                None,
            )

    async def save_graph_draft(
        self,
        draft: RelationshipGraphDraft,
        *,
        expected_revision: int,
    ) -> RelationshipGraphDraft:
        key = (draft.tenant_id, draft.graph_id)
        async with self._lock:
            current = self._graph_drafts.get(key)
            if current is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "relationship graph draft was not found",
                )
            if current.revision != expected_revision or draft.revision != expected_revision + 1:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.GRAPH_REVISION_CONFLICT,
                    "relationship graph draft revision is stale",
                )
            if (
                current.source_id != draft.source_id
                or current.source_snapshot_version != draft.source_snapshot_version
                or current.schema_fingerprint != draft.schema_fingerprint
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.VERSION_CONFLICT,
                    "relationship graph draft cannot change its source snapshot",
                )
            self._graph_drafts[key] = draft
            return draft

    async def save_recommendation_run(
        self, run: RelationshipRecommendationRun
    ) -> RelationshipRecommendationRun:
        key = (run.tenant_id, run.run_id)
        async with self._lock:
            draft = self._graph_drafts.get((run.tenant_id, run.graph_id))
            if draft is None or (
                draft.source_id != run.source_id
                or draft.source_snapshot_version != run.source_snapshot_version
                or draft.schema_fingerprint != run.schema_fingerprint
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "recommendation run graph draft was not found",
                )
            current = self._recommendation_runs.get(key)
            if current is not None and current.source_id != run.source_id:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.VERSION_CONFLICT,
                    "recommendation run belongs to another datasource",
                )
            self._recommendation_runs[key] = run
            return run

    async def get_recommendation_run(
        self, *, tenant_id: str, run_id: str
    ) -> RelationshipRecommendationRun | None:
        async with self._lock:
            return self._recommendation_runs.get((tenant_id, run_id))

    async def activate_graph_binding(
        self, binding: SemanticGraphBindingRecord
    ) -> SemanticGraphBindingRecord:
        key = (binding.tenant_id, binding.binding_id)
        async with self._lock:
            snapshot = self._snapshots.get((binding.tenant_id, binding.source_id, binding.source_snapshot_version))
            if snapshot is None or snapshot.fingerprint != binding.schema_fingerprint:
                raise DataSourceRegistryError(DataSourceRegistryErrorCode.GRAPH_STALE_SNAPSHOT, "graph binding source snapshot is stale")
            if key in self._graph_bindings:
                raise DataSourceRegistryError(DataSourceRegistryErrorCode.ALREADY_EXISTS, "graph binding already exists")
            now = datetime.now(UTC)
            for candidate_key, candidate in tuple(self._bindings.items()):
                if (
                    candidate.tenant_id == binding.tenant_id
                    and candidate.source_id == binding.source_id
                    and candidate.domain_id == binding.domain_id
                    and candidate.status == SemanticBindingStatus.ACTIVE
                ):
                    self._bindings[candidate_key] = candidate.model_copy(
                        update={
                            "status": SemanticBindingStatus.RETIRED,
                            "updated_at": now,
                        }
                    )
            for candidate_key, candidate in tuple(self._graph_bindings.items()):
                if candidate.tenant_id == binding.tenant_id and candidate.source_id == binding.source_id and candidate.domain_id == binding.domain_id and candidate.status == SemanticBindingStatus.ACTIVE:
                    self._graph_bindings[candidate_key] = candidate.model_copy(update={"status": SemanticBindingStatus.RETIRED, "updated_at": now})
            active = binding.model_copy(update={"status": SemanticBindingStatus.ACTIVE, "updated_at": now})
            self._graph_bindings[key] = active
            return active

    async def list_graph_bindings(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[SemanticGraphBindingRecord, ...]:
        async with self._lock:
            return tuple(
                binding
                for (candidate_tenant, _), binding in sorted(
                    self._graph_bindings.items()
                )
                if candidate_tenant == tenant_id
                and binding.source_id == source_id
            )

    async def pin_conversation(
        self,
        pin: ConversationDataSourcePin,
    ) -> ConversationDataSourcePin:
        key = (pin.tenant_id, pin.user_id, pin.conversation_id)
        async with self._lock:
            current = self._conversation_pins.get(key)
            if current is not None and current != pin:
                comparable = current.model_dump(exclude={"created_at"})
                requested = pin.model_dump(exclude={"created_at"})
                if comparable != requested:
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.VERSION_CONFLICT,
                        "conversation is already pinned to another datasource binding",
                    )
                return current
            self._conversation_pins[key] = pin
            return pin

    async def get_conversation_pin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationDataSourcePin | None:
        async with self._lock:
            return self._conversation_pins.get(
                (tenant_id, user_id, conversation_id)
            )

    def _active_binding_authority(
        self,
        *,
        tenant_id: str,
        source_id: str,
        binding_id: str,
        binding_version: int,
    ) -> SemanticBindingRecord | SemanticGraphBindingRecord | None:
        binding = self._bindings.get((tenant_id, binding_id))
        if binding is None:
            binding = self._graph_bindings.get((tenant_id, binding_id))
        if (
            binding is None
            or binding.source_id != source_id
            or binding.version != binding_version
            or binding.status != SemanticBindingStatus.ACTIVE
        ):
            return None
        return binding

    async def save_metric_proposal(self, proposal: MetricProposal) -> MetricProposal:
        key = (proposal.tenant_id, proposal.proposal_id)
        async with self._lock:
            if key in self._metric_proposals:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "metric proposal already exists",
                )
            snapshot = self._snapshots.get(
                (
                    proposal.tenant_id,
                    proposal.source_id,
                    proposal.source_snapshot_version,
                )
            )
            binding = self._active_binding_authority(
                tenant_id=proposal.tenant_id,
                source_id=proposal.source_id,
                binding_id=proposal.base_binding_id,
                binding_version=proposal.base_binding_version,
            )
            if (
                snapshot is None
                or snapshot.fingerprint != proposal.schema_fingerprint
                or binding is None
                or binding.domain_id != proposal.domain_id
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric proposal authority is stale or unavailable",
                )
            if proposal.revision != 1:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "new metric proposals must start at revision 1",
                )
            self._metric_proposals[key] = proposal
            return proposal

    async def update_metric_proposal(
        self,
        proposal: MetricProposal,
        *,
        expected_revision: int,
    ) -> MetricProposal:
        key = (proposal.tenant_id, proposal.proposal_id)
        async with self._lock:
            current = self._metric_proposals.get(key)
            if current is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "metric proposal was not found",
                )
            if (
                current.revision != expected_revision
                or proposal.revision != expected_revision + 1
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "metric proposal revision is stale",
                )
            immutable = (
                "tenant_id",
                "source_id",
                "source_snapshot_version",
                "schema_fingerprint",
                "domain_id",
                "base_binding_id",
                "base_binding_version",
                "requested_term",
                "created_by",
                "created_at",
            )
            if any(getattr(current, field) != getattr(proposal, field) for field in immutable):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric proposal cannot change its base authority",
                )
            self._metric_proposals[key] = proposal
            return proposal

    async def get_metric_proposal(
        self,
        *,
        tenant_id: str,
        proposal_id: str,
    ) -> MetricProposal | None:
        async with self._lock:
            return self._metric_proposals.get((tenant_id, proposal_id))

    async def list_metric_proposals(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[MetricProposal, ...]:
        async with self._lock:
            return tuple(
                proposal
                for (_, _), proposal in sorted(self._metric_proposals.items())
                if proposal.tenant_id == tenant_id and proposal.source_id == source_id
            )

    async def save_metric_validation_report(
        self,
        report: MetricValidationReport,
    ) -> MetricValidationReport:
        key = (report.tenant_id, report.report_id)
        async with self._lock:
            if key in self._metric_validation_reports:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "metric validation report already exists",
                )
            proposal = self._metric_proposals.get(
                (report.tenant_id, report.proposal_id)
            )
            candidate = next(
                (
                    item
                    for item in proposal.candidates
                    if item.candidate_id == report.candidate_id
                ),
                None,
            ) if proposal is not None else None
            if (
                proposal is None
                or proposal.revision != report.proposal_revision
                or proposal.content_digest != report.proposal_digest
                or candidate is None
                or semantic_digest(candidate.definition) != report.definition_digest
                or proposal.source_id != report.source_id
                or proposal.source_snapshot_version != report.source_snapshot_version
                or proposal.schema_fingerprint != report.schema_fingerprint
                or proposal.base_binding_id != report.binding_id
                or proposal.base_binding_version != report.binding_version
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric validation report does not match its proposal authority",
                )
            self._metric_validation_reports[key] = report
            return report

    async def get_metric_validation_report(
        self,
        *,
        tenant_id: str,
        report_id: str,
    ) -> MetricValidationReport | None:
        async with self._lock:
            return self._metric_validation_reports.get((tenant_id, report_id))

    async def save_metric_set(self, metric_set: MetricSetRecord) -> MetricSetRecord:
        key = (metric_set.tenant_id, metric_set.metric_set_id, metric_set.version)
        async with self._lock:
            if key in self._metric_sets:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "metric set version already exists",
                )
            snapshot = self._snapshots.get(
                (
                    metric_set.tenant_id,
                    metric_set.source_id,
                    metric_set.source_snapshot_version,
                )
            )
            binding = self._active_binding_authority(
                tenant_id=metric_set.tenant_id,
                source_id=metric_set.source_id,
                binding_id=metric_set.binding_id,
                binding_version=metric_set.binding_version,
            )
            if (
                snapshot is None
                or snapshot.fingerprint != metric_set.schema_fingerprint
                or binding is None
                or binding.domain_id != metric_set.domain_id
                or metric_set.status != MetricSetStatus.DRAFT
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric set authority is stale or the new version is not a draft",
                )
            prior_versions = [
                item.version
                for item in self._metric_sets.values()
                if item.tenant_id == metric_set.tenant_id
                and item.metric_set_id == metric_set.metric_set_id
            ]
            if metric_set.version != (max(prior_versions, default=0) + 1):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "metric set version is not the next version",
                )
            logical_refs = {item.logical_ref for item in binding.mappings}
            unknown = sorted(
                {
                    ref
                    for definition in metric_set.definitions
                    for ref in definition.all_field_refs
                    if ref not in logical_refs
                }
            )
            if unknown:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric set references unknown logical fields: " + ", ".join(unknown),
                )
            self._metric_sets[key] = metric_set
            return metric_set

    async def get_metric_set(
        self,
        *,
        tenant_id: str,
        metric_set_id: str,
        version: int,
    ) -> MetricSetRecord | None:
        async with self._lock:
            return self._metric_sets.get((tenant_id, metric_set_id, version))

    async def list_metric_sets(
        self,
        *,
        tenant_id: str,
        source_id: str,
        domain_id: str,
    ) -> tuple[MetricSetRecord, ...]:
        async with self._lock:
            return tuple(
                item
                for _, item in sorted(self._metric_sets.items())
                if item.tenant_id == tenant_id
                and item.source_id == source_id
                and item.domain_id == domain_id
            )

    async def activate_metric_set(
        self,
        *,
        tenant_id: str,
        metric_set_id: str,
        version: int,
        expected_pointer_revision: int,
    ) -> ActiveMetricSetPointer:
        async with self._lock:
            key = (tenant_id, metric_set_id, version)
            metric_set = self._metric_sets.get(key)
            if metric_set is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "metric set version was not found",
                )
            if metric_set.status == MetricSetStatus.REVOKED:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_SET_REVOKED,
                    "revoked metric sets cannot be activated",
                )
            pointer_key = (
                tenant_id,
                metric_set.source_id,
                metric_set.domain_id,
            )
            current = self._active_metric_sets.get(pointer_key)
            current_revision = current.revision if current is not None else 0
            if current_revision != expected_pointer_revision:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "active metric set pointer revision is stale",
                )
            if metric_set.status != MetricSetStatus.PUBLISHED:
                metric_set = metric_set.model_copy(
                    update={
                        "status": MetricSetStatus.PUBLISHED,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self._metric_sets[key] = metric_set
            pointer = ActiveMetricSetPointer(
                tenant_id=tenant_id,
                source_id=metric_set.source_id,
                domain_id=metric_set.domain_id,
                binding_id=metric_set.binding_id,
                binding_version=metric_set.binding_version,
                metric_set_id=metric_set.metric_set_id,
                metric_set_version=metric_set.version,
                metric_set_digest=metric_set.content_digest,
                revision=current_revision + 1,
            )
            self._active_metric_sets[pointer_key] = pointer
            return pointer

    async def get_active_metric_set(
        self,
        *,
        tenant_id: str,
        source_id: str,
        domain_id: str,
    ) -> ActiveMetricSetPointer | None:
        async with self._lock:
            return self._active_metric_sets.get((tenant_id, source_id, domain_id))

    async def save_metric_overlay(self, overlay: MetricOverlay) -> MetricOverlay:
        key = (overlay.tenant_id, overlay.overlay_id)
        async with self._lock:
            if key in self._metric_overlays:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "metric overlay already exists",
                )
            proposal = self._metric_proposals.get(
                (overlay.tenant_id, overlay.proposal_id)
            )
            report = self._metric_validation_reports.get(
                (overlay.tenant_id, overlay.validation_report_id)
            )
            candidate = next(
                (
                    item
                    for item in proposal.candidates
                    if item.candidate_id == proposal.selected_candidate_id
                ),
                None,
            ) if proposal is not None else None
            context = overlay.base_context
            if (
                proposal is None
                or proposal.revision != overlay.proposal_revision
                or proposal.content_digest != overlay.proposal_digest
                or candidate is None
                or candidate.definition != overlay.definition
                or report is None
                or report.digest != overlay.validation_report_digest
                or not report.activation_allowed
                or report.candidate_id != candidate.candidate_id
                or context.tenant_id != proposal.tenant_id
                or context.source_id != proposal.source_id
                or context.source_version != proposal.source_snapshot_version
                or context.binding_id != proposal.base_binding_id
                or context.binding_version != proposal.base_binding_version
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric overlay does not match an approved validation authority",
                )
            if context.metric_set is not None:
                metric_set = self._metric_sets.get(
                    (
                        overlay.tenant_id,
                        context.metric_set.metric_set_id,
                        context.metric_set.version,
                    )
                )
                if metric_set is None or any(
                    item.metric_ref.casefold() == overlay.definition.metric_ref.casefold()
                    for item in metric_set.definitions
                ):
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                        "metric overlay cannot shadow a governed metric",
                    )
            self._metric_overlays[key] = overlay
            return overlay

    async def get_metric_overlay(
        self,
        *,
        tenant_id: str,
        overlay_id: str,
    ) -> MetricOverlay | None:
        async with self._lock:
            return self._metric_overlays.get((tenant_id, overlay_id))

    async def get_run_metric_overlay(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> MetricOverlay | None:
        async with self._lock:
            matches = tuple(
                overlay
                for (candidate_tenant, _), overlay in self._metric_overlays.items()
                if candidate_tenant == tenant_id
                and overlay.user_id == user_id
                and overlay.scope == "run"
                and overlay.run_id == run_id
                and overlay.revoked_at is None
                and overlay.expires_at > datetime.now(UTC)
            )
            if len(matches) > 1:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "run has more than one active metric overlay",
                )
            return matches[0] if matches else None

    async def put_conversation_metric_pin(
        self,
        pin: ConversationMetricPin,
        *,
        expected_revision: int,
    ) -> ConversationMetricPin:
        key = (pin.tenant_id, pin.user_id, pin.conversation_id)
        async with self._lock:
            current = self._conversation_metric_pins.get(key)
            current_revision = current.revision if current is not None else 0
            if (
                current_revision != expected_revision
                or pin.revision != expected_revision + 1
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "conversation metric pin revision is stale",
                )
            if current is not None and pin.created_at != current.created_at:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "conversation metric repin must preserve created_at",
                )
            context = pin.context
            if context.metric_set is not None:
                metric_set = self._metric_sets.get(
                    (
                        pin.tenant_id,
                        context.metric_set.metric_set_id,
                        context.metric_set.version,
                    )
                )
                if (
                    metric_set is None
                    or metric_set.content_digest != context.metric_set.digest
                    or metric_set.status == MetricSetStatus.REVOKED
                    or metric_set.source_id != context.source_id
                    or metric_set.binding_id != context.binding_id
                    or metric_set.domain_id != pin.domain_id
                ):
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                        "conversation metric set pin is unavailable or stale",
                    )
            if context.overlay_id is not None:
                overlay = self._metric_overlays.get(
                    (pin.tenant_id, context.overlay_id)
                )
                if (
                    overlay is None
                    or overlay.content_digest != context.overlay_digest
                    or overlay.user_id != pin.user_id
                    or overlay.scope != "conversation"
                    or overlay.conversation_id != pin.conversation_id
                    or overlay.revoked_at is not None
                    or overlay.expires_at <= datetime.now(UTC)
                ):
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                        "conversation metric overlay is unavailable or stale",
                    )
            self._conversation_metric_pins[key] = pin
            return pin

    async def get_conversation_metric_pin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationMetricPin | None:
        async with self._lock:
            return self._conversation_metric_pins.get(
                (tenant_id, user_id, conversation_id)
            )

    async def put_domain_pack_assignment(
        self,
        assignment: DomainPackAssignment,
        *,
        expected_revision: int,
    ) -> DomainPackAssignment:
        key = (assignment.tenant_id, assignment.source_id, assignment.domain_id)
        async with self._lock:
            if (assignment.tenant_id, assignment.source_id) not in self._sources:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "domain pack assignment datasource was not found",
                )
            current = self._domain_pack_assignments.get(key)
            current_revision = current.revision if current is not None else 0
            if (
                current_revision != expected_revision
                or assignment.revision != expected_revision + 1
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "domain pack assignment revision is stale",
                )
            if current is not None and assignment.created_at != current.created_at:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "domain pack reassignment must preserve created_at",
                )
            self._domain_pack_assignments[key] = assignment
            return assignment

    async def get_domain_pack_assignment(
        self,
        *,
        tenant_id: str,
        source_id: str,
        domain_id: str,
    ) -> DomainPackAssignment | None:
        async with self._lock:
            return self._domain_pack_assignments.get(
                (tenant_id, source_id, domain_id)
            )

    async def append_semantic_audit_event(
        self,
        event: SemanticAuditEvent,
    ) -> SemanticAuditEvent:
        key = (event.tenant_id, event.event_id)
        async with self._lock:
            if key in self._semantic_audit_events:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "semantic audit event already exists",
                )
            self._semantic_audit_events[key] = event
            return event

    async def list_semantic_audit_events(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
    ) -> tuple[SemanticAuditEvent, ...]:
        async with self._lock:
            return tuple(
                event
                for _, event in sorted(
                    self._semantic_audit_events.items(),
                    key=lambda item: (item[1].created_at, item[1].event_id),
                )
                if event.tenant_id == tenant_id
                and event.resource_type == resource_type
                and event.resource_id == resource_id
            )


class ConnectorRegistry:
    """Bind immutable datasource versions to connector instances."""

    _EXPECTED_DIALECT = {
        DataSourceKind.POSTGRES: "postgres",
        DataSourceKind.SQLITE: "sqlite",
        DataSourceKind.XLSX: "duckdb",
        DataSourceKind.CSV: "duckdb",
    }

    def __init__(self) -> None:
        self._connectors: dict[tuple[str, str, int], DataSourceConnector] = {}

    def register(
        self,
        definition: DataSourceDefinition,
        connector: DataSourceConnector,
        *,
        source_version: int,
    ) -> None:
        key = (definition.tenant_id, definition.source_id, source_version)
        if key in self._connectors:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.ALREADY_EXISTS,
                "connector is already registered for this datasource version",
            )
        capabilities = connector.capabilities()
        if (
            not capabilities.read_only
            or capabilities.dialect != self._EXPECTED_DIALECT[definition.kind]
        ):
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.CONNECTOR_MISMATCH,
                "connector capabilities do not match datasource kind",
            )
        self._connectors[key] = connector

    def get(
        self,
        *,
        tenant_id: str,
        source_id: str,
        source_version: int,
    ) -> DataSourceConnector:
        connector = self._connectors.get(
            (tenant_id, source_id, source_version)
        )
        if connector is None:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.NOT_FOUND,
                "connector was not found for this datasource version",
            )
        return connector

    def remove(self, *, tenant_id: str, source_id: str) -> int:
        keys = tuple(
            key
            for key in self._connectors
            if key[0] == tenant_id and key[1] == source_id
        )
        for key in keys:
            self._connectors.pop(key, None)
        return len(keys)


__all__ = [
    "ConnectorRegistry",
    "DataSourceRegistry",
    "DataSourceRegistryError",
    "DataSourceRegistryErrorCode",
    "InMemoryDataSourceRegistry",
]
