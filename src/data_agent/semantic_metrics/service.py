"""Governance service for metric proposals, validation, overlays, and release."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from data_agent.datasources.models import SemanticBindingStatus

from .catalog import EffectiveMetricCatalog, MetricCatalogEntry, MetricCatalogOrigin
from .digest import semantic_digest
from .models import (
    ActiveMetricSetPointer,
    ConversationMetricPin,
    DomainPackIdentity,
    MetricContextPin,
    MetricOverlay,
    MetricProposal,
    MetricProposalCandidate,
    MetricProposalStatus,
    MetricRiskTier,
    MetricSetIdentity,
    MetricSetRecord,
    MetricValidationReport,
    SemanticAuditEvent,
)
from .validator import SemanticMetricStaticValidator
from .feature_flags import SemanticMetricFeatures


class SemanticMetricServiceErrorCode(StrEnum):
    PERMISSION_DENIED = "METRIC_PERMISSION_DENIED"
    NOT_FOUND = "METRIC_NOT_FOUND"
    INVALID_STATE = "INVALID_METRIC_STATE"
    STALE_AUTHORITY = "METRIC_STALE_AUTHORITY"
    VALIDATION_FAILED = "METRIC_VALIDATION_FAILED"
    CONFLICT = "METRIC_CONFLICT"


class SemanticMetricServiceError(RuntimeError):
    def __init__(self, code: SemanticMetricServiceErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SemanticMetricActor:
    tenant_id: str
    user_id: str
    roles: tuple[str, ...] = ()

    def has_any_role(self, allowed: set[str]) -> bool:
        return bool({role.casefold() for role in self.roles} & allowed)


_PROPOSER_ROLES = {
    "user",
    "analyst",
    "data_analyst",
    "semantic_editor",
    "data_admin",
    "semantic_admin",
    "admin",
    "enterprise_admin",
}
_VALIDATOR_ROLES = {
    "semantic_editor",
    "data_admin",
    "semantic_admin",
    "admin",
    "enterprise_admin",
    "system",
}
_APPROVER_ROLES = {
    "data_admin",
    "semantic_admin",
    "admin",
    "enterprise_admin",
}


class SemanticMetricGovernanceService:
    """Enforce lifecycle, authority pins, and RBAC above the persistence layer."""

    def __init__(
        self,
        registry: Any,
        *,
        web_discovery: Any | None = None,
        features: SemanticMetricFeatures | None = None,
    ) -> None:
        self._registry = registry
        self._validator = SemanticMetricStaticValidator()
        self._web_discovery = web_discovery
        self._features = features or SemanticMetricFeatures(
            web_discovery=web_discovery is not None
        )

    async def list_proposals(
        self,
        *,
        actor: SemanticMetricActor,
        source_id: str,
    ) -> tuple[MetricProposal, ...]:
        self._require_role(actor, _PROPOSER_ROLES | _APPROVER_ROLES)
        return await self._registry.list_metric_proposals(
            tenant_id=actor.tenant_id,
            source_id=source_id,
        )

    async def get_proposal(
        self,
        *,
        actor: SemanticMetricActor,
        proposal_id: str,
    ) -> MetricProposal:
        self._require_role(actor, _PROPOSER_ROLES | _APPROVER_ROLES)
        return await self._proposal(actor, proposal_id)

    async def get_active_pointer(
        self,
        *,
        actor: SemanticMetricActor,
        source_id: str,
        domain_id: str,
    ) -> ActiveMetricSetPointer | None:
        self._require_role(actor, _PROPOSER_ROLES | _APPROVER_ROLES)
        return await self._registry.get_active_metric_set(
            tenant_id=actor.tenant_id,
            source_id=source_id,
            domain_id=domain_id,
        )

    async def create_proposal(
        self,
        *,
        actor: SemanticMetricActor,
        source_id: str,
        domain_id: str,
        requested_term: str,
        candidates: tuple[MetricProposalCandidate, ...],
        risk_tier: MetricRiskTier = MetricRiskTier.HIGH,
        domain_pack: DomainPackIdentity | None = None,
    ) -> MetricProposal:
        self._require_role(actor, _PROPOSER_ROLES)
        snapshot, binding = await self._authority(
            actor.tenant_id, source_id, domain_id
        )
        await self._ensure_candidates_do_not_shadow(
            actor.tenant_id, source_id, domain_id, candidates
        )
        proposal = MetricProposal(
            proposal_id=f"proposal-{uuid4().hex}",
            tenant_id=actor.tenant_id,
            source_id=source_id,
            source_snapshot_version=snapshot.version,
            schema_fingerprint=snapshot.fingerprint,
            domain_id=domain_id,
            base_binding_id=binding.binding_id,
            base_binding_version=binding.version,
            requested_term=requested_term,
            risk_tier=risk_tier,
            domain_pack=domain_pack,
            candidates=candidates,
            created_by=actor.user_id,
        )
        saved = await self._registry.save_metric_proposal(proposal)
        await self._audit(actor, "metric.proposal.created", saved, source_id)
        return saved

    async def discover_proposal(
        self,
        *,
        actor: SemanticMetricActor,
        source_id: str,
        domain_id: str,
        requested_term: str,
    ) -> MetricProposal:
        from .domain_packs import DomainPackRegistry

        self._require_role(actor, _PROPOSER_ROLES)
        snapshot, binding = await self._authority(
            actor.tenant_id, source_id, domain_id
        )
        registry = DomainPackRegistry()
        matches = (
            registry.detect_templates(requested_term, domain_id=domain_id)
            if self._features.domain_pack_discovery
            else ()
        )
        candidates = (
            registry.propose(
                requested_term=requested_term,
                binding=binding,
                catalog=snapshot.catalog,
                domain_id=domain_id,
            )
            if matches
            else ()
        )
        if candidates:
            manifest, template, _ = matches[0]
            domain_pack = DomainPackIdentity(
                pack_id=manifest.pack_id,
                version=manifest.version,
                digest=manifest.digest,
                domain_id=manifest.domain_id,
            )
            risk_tier = template.risk_tier
        elif self._features.web_discovery and self._web_discovery is not None:
            candidates = await self._web_discovery.discover(
                requested_term=requested_term,
                domain_id=domain_id,
                logical_fields=tuple(
                    {
                        "ref": item.logical_ref,
                        "role": item.semantic_role,
                        "entity": item.entity,
                        "unit": item.unit,
                    }
                    for item in binding.mappings
                ),
            )
            domain_pack = None
            risk_tier = MetricRiskTier.HIGH
        else:
            candidates = ()
            domain_pack = None
            risk_tier = MetricRiskTier.HIGH
        if not candidates:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.NOT_FOUND,
                "neither installed domain packs nor controlled web discovery "
                "could ground this term to the active schema",
            )
        return await self.create_proposal(
            actor=actor,
            source_id=source_id,
            domain_id=domain_id,
            requested_term=requested_term,
            candidates=candidates,
            risk_tier=risk_tier,
            domain_pack=domain_pack,
        )

    async def discover_or_reuse_proposal(
        self,
        *,
        actor: SemanticMetricActor,
        source_id: str,
        domain_id: str,
        requested_term: str,
    ) -> MetricProposal:
        """Reuse an authority-identical open draft across Agent retries."""

        self._require_role(actor, _PROPOSER_ROLES)
        snapshot, binding = await self._authority(
            actor.tenant_id, source_id, domain_id
        )
        open_statuses = {
            MetricProposalStatus.DRAFT,
            MetricProposalStatus.NEEDS_CLARIFICATION,
            MetricProposalStatus.VALIDATED,
            MetricProposalStatus.PENDING_APPROVAL,
        }
        normalized_term = requested_term.strip().casefold()
        proposals = await self._registry.list_metric_proposals(
            tenant_id=actor.tenant_id,
            source_id=source_id,
        )
        reusable = next(
            (
                proposal
                for proposal in proposals
                if proposal.created_by == actor.user_id
                and proposal.domain_id == domain_id
                and proposal.requested_term.strip().casefold() == normalized_term
                and proposal.status in open_statuses
                and proposal.source_snapshot_version == snapshot.version
                and proposal.schema_fingerprint == snapshot.fingerprint
                and proposal.base_binding_id == binding.binding_id
                and proposal.base_binding_version == binding.version
            ),
            None,
        )
        if reusable is not None:
            return reusable
        return await self.discover_proposal(
            actor=actor,
            source_id=source_id,
            domain_id=domain_id,
            requested_term=requested_term,
        )

    async def select_candidate(
        self,
        *,
        actor: SemanticMetricActor,
        proposal_id: str,
        candidate_id: str,
        expected_revision: int,
    ) -> MetricProposal:
        self._require_role(actor, _PROPOSER_ROLES)
        proposal = await self._proposal(actor, proposal_id)
        self._require_owner_or_admin(actor, proposal)
        if proposal.revision != expected_revision:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.CONFLICT,
                "metric proposal revision is stale",
            )
        candidate = next(
            (item for item in proposal.candidates if item.candidate_id == candidate_id),
            None,
        )
        if candidate is None:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.NOT_FOUND,
                "metric proposal candidate was not found",
            )
        if proposal.status not in {
            MetricProposalStatus.DRAFT,
            MetricProposalStatus.NEEDS_CLARIFICATION,
        }:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.INVALID_STATE,
                "metric candidate cannot be changed in the current state",
            )
        updated = proposal.model_copy(
            update={
                "revision": proposal.revision + 1,
                "selected_candidate_id": candidate_id,
                "status": (
                    MetricProposalStatus.NEEDS_CLARIFICATION
                    if candidate.required_decisions
                    else MetricProposalStatus.DRAFT
                ),
                "updated_at": datetime.now(UTC),
            }
        )
        saved = await self._registry.update_metric_proposal(
            updated, expected_revision=expected_revision
        )
        await self._audit(actor, "metric.proposal.selected", saved, saved.source_id)
        return saved

    async def revise_candidate(
        self,
        *,
        actor: SemanticMetricActor,
        proposal_id: str,
        candidate: MetricProposalCandidate,
        expected_revision: int,
    ) -> MetricProposal:
        self._require_role(actor, _PROPOSER_ROLES)
        proposal = await self._proposal(actor, proposal_id)
        self._require_owner_or_admin(actor, proposal)
        if proposal.revision != expected_revision:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.CONFLICT,
                "metric proposal revision is stale",
            )
        if proposal.status not in {
            MetricProposalStatus.DRAFT,
            MetricProposalStatus.NEEDS_CLARIFICATION,
        }:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.INVALID_STATE,
                "metric candidate cannot be revised in the current state",
            )
        existing = next(
            (item for item in proposal.candidates if item.candidate_id == candidate.candidate_id),
            None,
        )
        if existing is None:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.NOT_FOUND,
                "metric proposal candidate was not found",
            )
        candidates = tuple(
            candidate if item.candidate_id == candidate.candidate_id else item
            for item in proposal.candidates
        )
        updated = proposal.model_copy(
            update={
                "revision": proposal.revision + 1,
                "candidates": candidates,
                "selected_candidate_id": candidate.candidate_id,
                "status": (
                    MetricProposalStatus.NEEDS_CLARIFICATION
                    if candidate.required_decisions
                    else MetricProposalStatus.DRAFT
                ),
                "updated_at": datetime.now(UTC),
            }
        )
        saved = await self._registry.update_metric_proposal(
            updated, expected_revision=expected_revision
        )
        await self._audit(actor, "metric.proposal.revised", saved, saved.source_id)
        return saved

    async def validate_proposal(
        self,
        *,
        actor: SemanticMetricActor,
        proposal_id: str,
        expected_revision: int,
    ) -> tuple[MetricProposal, MetricValidationReport]:
        self._require_role(actor, _VALIDATOR_ROLES)
        proposal = await self._proposal(actor, proposal_id)
        if proposal.revision != expected_revision:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.CONFLICT,
                "metric proposal revision is stale",
            )
        candidate = self._selected_candidate(proposal)
        if candidate.required_decisions:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.INVALID_STATE,
                "required metric scope decisions must be resolved before validation",
            )
        snapshot, binding = await self._authority(
            actor.tenant_id, proposal.source_id, proposal.domain_id
        )
        self._require_proposal_authority(proposal, snapshot, binding)
        issues = self._validator.validate(
            candidate.definition,
            binding=binding,
            catalog=snapshot.catalog,
        )
        allowed = not any(item.severity == "error" for item in issues)
        updated = proposal.model_copy(
            update={
                "revision": proposal.revision + 1,
                "status": (
                    MetricProposalStatus.PENDING_APPROVAL
                    if allowed
                    else MetricProposalStatus.NEEDS_CLARIFICATION
                ),
                "updated_at": datetime.now(UTC),
            }
        )
        saved = await self._registry.update_metric_proposal(
            updated, expected_revision=expected_revision
        )
        report = MetricValidationReport(
            report_id=f"metric-validation-{uuid4().hex}",
            tenant_id=actor.tenant_id,
            proposal_id=saved.proposal_id,
            proposal_revision=saved.revision,
            proposal_digest=saved.content_digest,
            candidate_id=candidate.candidate_id,
            definition_digest=semantic_digest(candidate.definition),
            source_id=saved.source_id,
            source_snapshot_version=saved.source_snapshot_version,
            schema_fingerprint=saved.schema_fingerprint,
            binding_id=saved.base_binding_id,
            binding_version=saved.base_binding_version,
            validator_digest=self._validator.digest,
            issues=issues,
        )
        stored_report = await self._registry.save_metric_validation_report(report)
        await self._audit(
            actor,
            "metric.proposal.validated",
            saved,
            saved.source_id,
            {"report_digest": stored_report.digest, "allowed": allowed},
        )
        return saved, stored_report

    async def create_overlay(
        self,
        *,
        actor: SemanticMetricActor,
        proposal_id: str,
        validation_report_id: str,
        scope: str,
        run_id: str | None = None,
        conversation_id: str | None = None,
        ttl: timedelta = timedelta(hours=4),
    ) -> MetricOverlay:
        if not self._features.provisional_overlays:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.INVALID_STATE,
                "provisional metric overlays are disabled by rollout policy",
            )
        self._require_role(actor, _PROPOSER_ROLES)
        proposal = await self._proposal(actor, proposal_id)
        self._require_owner_or_admin(actor, proposal)
        candidate = self._selected_candidate(proposal)
        report = await self._registry.get_metric_validation_report(
            tenant_id=actor.tenant_id,
            report_id=validation_report_id,
        )
        if (
            report is None
            or not report.activation_allowed
            or report.proposal_id != proposal.proposal_id
            or report.proposal_revision != proposal.revision
            or report.proposal_digest != proposal.content_digest
            or report.candidate_id != candidate.candidate_id
        ):
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.VALIDATION_FAILED,
                "metric overlay requires the current successful validation report",
            )
        if scope not in {"run", "conversation"}:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.INVALID_STATE,
                "metric overlay scope must be run or conversation",
            )
        current_pin = None
        if scope == "run":
            assert run_id is not None
            existing_run_overlay = await self._registry.get_run_metric_overlay(
                tenant_id=actor.tenant_id,
                user_id=actor.user_id,
                run_id=run_id,
            )
            if existing_run_overlay is not None:
                raise SemanticMetricServiceError(
                    SemanticMetricServiceErrorCode.CONFLICT,
                    "run already has an active metric overlay",
                )
            context = await self._metric_context(proposal)
        else:
            assert conversation_id is not None
            current_pin = await self._registry.get_conversation_metric_pin(
                tenant_id=actor.tenant_id,
                user_id=actor.user_id,
                conversation_id=conversation_id,
            )
            if current_pin is not None:
                context = current_pin.context
                if (
                    current_pin.domain_id != proposal.domain_id
                    or context.source_id != proposal.source_id
                    or context.source_version != proposal.source_snapshot_version
                    or context.binding_id != proposal.base_binding_id
                    or context.binding_version != proposal.base_binding_version
                    or context.overlay_id is not None
                ):
                    raise SemanticMetricServiceError(
                        SemanticMetricServiceErrorCode.STALE_AUTHORITY,
                        "conversation metric authority is stale or already has an overlay",
                    )
            else:
                context = await self._metric_context(proposal)
        overlay = MetricOverlay(
            overlay_id=f"overlay-{uuid4().hex}",
            tenant_id=actor.tenant_id,
            user_id=actor.user_id,
            scope=scope,
            run_id=run_id,
            conversation_id=conversation_id,
            base_context=context,
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            proposal_digest=proposal.content_digest,
            definition=candidate.definition,
            validation_report_id=report.report_id,
            validation_report_digest=report.digest,
            expires_at=datetime.now(UTC) + ttl,
        )
        saved = await self._registry.save_metric_overlay(overlay)
        if scope == "run":
            await self._audit(actor, "metric.overlay.created", saved, proposal.source_id)
            return saved
        assert conversation_id is not None
        pinned_context = context.model_copy(
            update={
                "overlay_id": saved.overlay_id,
                "overlay_digest": saved.content_digest,
                "revision": context.revision + 1,
            }
        )
        now = datetime.now(UTC)
        pin = ConversationMetricPin(
            tenant_id=actor.tenant_id,
            user_id=actor.user_id,
            conversation_id=conversation_id,
            domain_id=proposal.domain_id,
            context=pinned_context,
            revision=(current_pin.revision + 1 if current_pin is not None else 1),
            created_at=(current_pin.created_at if current_pin is not None else now),
            updated_at=now,
        )
        try:
            await self._registry.put_conversation_metric_pin(
                pin,
                expected_revision=(current_pin.revision if current_pin is not None else 0),
            )
        except RuntimeError as exc:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.CONFLICT,
                "conversation metric pin changed while creating the overlay",
            ) from exc
        await self._audit(actor, "metric.overlay.created", saved, proposal.source_id)
        return saved

    async def approve_and_activate(
        self,
        *,
        actor: SemanticMetricActor,
        proposal_id: str,
        validation_report_id: str,
        expected_revision: int,
        expected_pointer_revision: int,
    ) -> tuple[MetricProposal, MetricSetRecord, ActiveMetricSetPointer]:
        self._require_role(actor, _APPROVER_ROLES)
        proposal = await self._proposal(actor, proposal_id)
        if proposal.revision != expected_revision:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.CONFLICT,
                "metric proposal revision is stale",
            )
        if proposal.status != MetricProposalStatus.PENDING_APPROVAL:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.INVALID_STATE,
                "only validated pending proposals can be approved",
            )
        candidate = self._selected_candidate(proposal)
        report = await self._registry.get_metric_validation_report(
            tenant_id=actor.tenant_id,
            report_id=validation_report_id,
        )
        if (
            report is None
            or not report.activation_allowed
            or report.proposal_id != proposal.proposal_id
            or report.proposal_revision != proposal.revision
            or report.proposal_digest != proposal.content_digest
            or report.definition_digest != semantic_digest(candidate.definition)
        ):
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.VALIDATION_FAILED,
                "approval requires the current successful validation report",
            )
        snapshot, binding = await self._authority(
            actor.tenant_id, proposal.source_id, proposal.domain_id
        )
        self._require_proposal_authority(proposal, snapshot, binding)
        current = await self._registry.get_active_metric_set(
            tenant_id=actor.tenant_id,
            source_id=proposal.source_id,
            domain_id=proposal.domain_id,
        )
        definitions = ()
        metric_set_id = f"metric-set-{uuid4().hex}"
        version = 1
        if current is not None:
            if current.revision != expected_pointer_revision:
                raise SemanticMetricServiceError(
                    SemanticMetricServiceErrorCode.CONFLICT,
                    "active metric set pointer revision is stale",
                )
            active_set = await self._registry.get_metric_set(
                tenant_id=actor.tenant_id,
                metric_set_id=current.metric_set_id,
                version=current.metric_set_version,
            )
            if active_set is None or active_set.content_digest != current.metric_set_digest:
                raise SemanticMetricServiceError(
                    SemanticMetricServiceErrorCode.STALE_AUTHORITY,
                    "active metric set is unavailable or stale",
                )
            definitions = active_set.definitions
            metric_set_id = active_set.metric_set_id
            version = active_set.version + 1
        elif expected_pointer_revision != 0:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.CONFLICT,
                "active metric set pointer revision is stale",
            )
        self._assert_definition_compatible(definitions, candidate)
        metric_set = MetricSetRecord.create(
            tenant_id=actor.tenant_id,
            source_id=proposal.source_id,
            source_snapshot_version=proposal.source_snapshot_version,
            schema_fingerprint=proposal.schema_fingerprint,
            domain_id=proposal.domain_id,
            binding_id=proposal.base_binding_id,
            binding_version=proposal.base_binding_version,
            metric_set_id=metric_set_id,
            version=version,
            definitions=(*definitions, candidate.definition),
            created_by=actor.user_id,
        )
        stored_set = await self._registry.save_metric_set(metric_set)
        pointer = await self._registry.activate_metric_set(
            tenant_id=actor.tenant_id,
            metric_set_id=stored_set.metric_set_id,
            version=stored_set.version,
            expected_pointer_revision=expected_pointer_revision,
        )
        published_set = await self._registry.get_metric_set(
            tenant_id=actor.tenant_id,
            metric_set_id=stored_set.metric_set_id,
            version=stored_set.version,
        )
        if published_set is None:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.STALE_AUTHORITY,
                "activated metric set could not be read back",
            )
        approved = proposal.model_copy(
            update={
                "revision": proposal.revision + 1,
                "status": MetricProposalStatus.APPROVED,
                "updated_at": datetime.now(UTC),
            }
        )
        saved_proposal = await self._registry.update_metric_proposal(
            approved, expected_revision=expected_revision
        )
        await self._audit(
            actor,
            "metric.set.activated",
            published_set,
            published_set.source_id,
            {
                "proposal_id": proposal.proposal_id,
                "report_digest": report.digest,
                "pointer_revision": pointer.revision,
            },
        )
        return saved_proposal, published_set, pointer

    async def _authority(self, tenant_id: str, source_id: str, domain_id: str):
        source = await self._registry.get(tenant_id=tenant_id, source_id=source_id)
        snapshot = await self._registry.get_snapshot(
            tenant_id=tenant_id,
            source_id=source_id,
            version=source.active_snapshot_version,
        )
        bindings = (
            *await self._registry.list_bindings(
                tenant_id=tenant_id, source_id=source_id
            ),
            *await self._registry.list_graph_bindings(
                tenant_id=tenant_id, source_id=source_id
            ),
        )
        active = [
            item
            for item in bindings
            if item.domain_id == domain_id
            and item.status == SemanticBindingStatus.ACTIVE
            and item.source_snapshot_version == snapshot.version
        ]
        if len(active) != 1:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.STALE_AUTHORITY,
                "exactly one active semantic binding is required",
            )
        return snapshot, active[0]

    @staticmethod
    def _require_proposal_authority(proposal, snapshot, binding) -> None:
        fingerprint = getattr(binding, "schema_fingerprint", snapshot.fingerprint)
        if (
            proposal.source_snapshot_version != snapshot.version
            or proposal.schema_fingerprint != snapshot.fingerprint
            or proposal.base_binding_id != binding.binding_id
            or proposal.base_binding_version != binding.version
            or fingerprint != snapshot.fingerprint
        ):
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.STALE_AUTHORITY,
                "metric proposal authority changed and must be revalidated",
            )

    async def _metric_context(self, proposal: MetricProposal) -> MetricContextPin:
        pointer = await self._registry.get_active_metric_set(
            tenant_id=proposal.tenant_id,
            source_id=proposal.source_id,
            domain_id=proposal.domain_id,
        )
        identity = (
            MetricSetIdentity(
                metric_set_id=pointer.metric_set_id,
                version=pointer.metric_set_version,
                digest=pointer.metric_set_digest,
            )
            if pointer is not None
            else None
        )
        return MetricContextPin(
            tenant_id=proposal.tenant_id,
            source_id=proposal.source_id,
            source_version=proposal.source_snapshot_version,
            binding_id=proposal.base_binding_id,
            binding_version=proposal.base_binding_version,
            metric_set=identity,
        )

    async def _ensure_candidates_do_not_shadow(
        self,
        tenant_id: str,
        source_id: str,
        domain_id: str,
        candidates: tuple[MetricProposalCandidate, ...],
    ) -> None:
        pointer = await self._registry.get_active_metric_set(
            tenant_id=tenant_id, source_id=source_id, domain_id=domain_id
        )
        if pointer is None:
            return
        metric_set = await self._registry.get_metric_set(
            tenant_id=tenant_id,
            metric_set_id=pointer.metric_set_id,
            version=pointer.metric_set_version,
        )
        if metric_set is None:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.STALE_AUTHORITY,
                "active metric set is unavailable",
            )
        governed = tuple(
            MetricCatalogEntry.create(
                definition=item,
                origin=MetricCatalogOrigin.GOVERNED,
                authority_ref=metric_set.content_digest,
            )
            for item in metric_set.definitions
        )
        for candidate in candidates:
            try:
                EffectiveMetricCatalog.build(
                    governed=governed,
                    overlays=(
                        MetricCatalogEntry.create(
                            definition=candidate.definition,
                            origin=MetricCatalogOrigin.OVERLAY,
                            authority_ref="proposal-candidate",
                        ),
                    ),
                )
            except Exception as exc:
                raise SemanticMetricServiceError(
                    SemanticMetricServiceErrorCode.CONFLICT,
                    "proposal candidate conflicts with an active metric",
                ) from exc

    @staticmethod
    def _assert_definition_compatible(
        definitions,
        candidate: MetricProposalCandidate,
    ) -> None:
        entries = tuple(
            MetricCatalogEntry.create(
                definition=item,
                origin=MetricCatalogOrigin.GOVERNED,
                authority_ref="pending-release",
            )
            for item in (*definitions, candidate.definition)
        )
        try:
            EffectiveMetricCatalog.build(governed=entries)
        except Exception as exc:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.CONFLICT,
                "approved metric conflicts with the active metric catalog",
            ) from exc

    async def _proposal(self, actor, proposal_id: str) -> MetricProposal:
        proposal = await self._registry.get_metric_proposal(
            tenant_id=actor.tenant_id, proposal_id=proposal_id
        )
        if proposal is None:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.NOT_FOUND,
                "metric proposal was not found",
            )
        return proposal

    @staticmethod
    def _selected_candidate(proposal: MetricProposal) -> MetricProposalCandidate:
        candidate = next(
            (
                item
                for item in proposal.candidates
                if item.candidate_id == proposal.selected_candidate_id
            ),
            None,
        )
        if candidate is None:
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.INVALID_STATE,
                "metric proposal requires a selected candidate",
            )
        return candidate

    @staticmethod
    def _require_role(actor: SemanticMetricActor, allowed: set[str]) -> None:
        if not actor.has_any_role(allowed):
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.PERMISSION_DENIED,
                "actor is not permitted to perform this semantic metric action",
            )

    @staticmethod
    def _require_owner_or_admin(actor, proposal: MetricProposal) -> None:
        if proposal.created_by != actor.user_id and not actor.has_any_role(
            _APPROVER_ROLES
        ):
            raise SemanticMetricServiceError(
                SemanticMetricServiceErrorCode.PERMISSION_DENIED,
                "only the proposal owner or a semantic administrator may modify it",
            )

    async def _audit(
        self,
        actor: SemanticMetricActor,
        action: str,
        resource,
        source_id: str,
        details: dict | None = None,
    ) -> None:
        resource_id = getattr(
            resource,
            "proposal_id",
            getattr(resource, "metric_set_id", getattr(resource, "overlay_id", "unknown")),
        )
        revision = getattr(resource, "revision", getattr(resource, "version", None))
        await self._registry.append_semantic_audit_event(
            SemanticAuditEvent(
                event_id=f"audit-{uuid4().hex}",
                tenant_id=actor.tenant_id,
                source_id=source_id,
                actor_id=actor.user_id,
                action=action,
                resource_type=type(resource).__name__.casefold(),
                resource_id=resource_id,
                resource_revision=revision,
                details=details or {},
            )
        )


__all__ = [
    "SemanticMetricActor",
    "SemanticMetricGovernanceService",
    "SemanticMetricServiceError",
    "SemanticMetricServiceErrorCode",
]
