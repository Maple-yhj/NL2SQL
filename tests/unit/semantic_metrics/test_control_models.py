from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from data_agent.semantic_metrics import (
    ConversationMetricPin,
    DomainPackAssignment,
    DomainPackIdentity,
    MetricAggregateFormula,
    MetricContextPin,
    MetricFieldExpression,
    MetricOverlay,
    MetricProposal,
    MetricProposalCandidate,
    MetricSetRecord,
    MetricValidationIssue,
    MetricValidationReport,
    MetricValidationSeverity,
    SemanticMetricDefinitionV2,
    semantic_digest,
)


def _definition() -> SemanticMetricDefinitionV2:
    return SemanticMetricDefinitionV2(
        metric_ref="commerce.gmv",
        display_name="GMV",
        description="Price based GMV",
        formula=MetricAggregateFormula(
            operation="sum",
            operand=MetricFieldExpression(ref="orders.price"),
        ),
        default_time_ref="orders.purchased_at",
        entity_key_refs=("orders.order_id",),
        currency="BRL",
    )


def _context() -> MetricContextPin:
    return MetricContextPin(
        tenant_id="tenant",
        source_id="olist",
        source_version=1,
        binding_id="olist-binding",
        binding_version=2,
    )


def _proposal() -> MetricProposal:
    definition = _definition()
    return MetricProposal(
        proposal_id="proposal-gmv",
        tenant_id="tenant",
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint="sha256:schema",
        domain_id="commerce",
        base_binding_id="olist-binding",
        base_binding_version=2,
        requested_term="GMV",
        candidates=(
            MetricProposalCandidate(
                candidate_id="price-only",
                definition=definition,
                label="Item price",
                rationale="Uses the item price measure",
                required_decisions=("Choose the event time",),
            ),
        ),
        created_by="analyst@example.com",
    )


def test_metric_set_digest_pins_data_and_binding_authority() -> None:
    record = MetricSetRecord.create(
        tenant_id="tenant",
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint="sha256:schema",
        domain_id="commerce",
        binding_id="olist-binding",
        binding_version=2,
        metric_set_id="commerce-metrics",
        version=1,
        definitions=(_definition(),),
        created_by="steward@example.com",
    )

    assert record.content_digest.startswith("sha256:")
    assert record.model_copy(update={"status": "published"}).content_digest == (
        record.content_digest
    )
    with pytest.raises(ValidationError, match="content digest"):
        record.model_copy(update={"binding_version": 3})


def test_proposal_requires_selection_before_validated_state() -> None:
    proposal = _proposal()
    assert proposal.content_digest == proposal.content_digest

    with pytest.raises(ValidationError, match="selected candidate"):
        proposal.model_copy(update={"status": "validated"})

    validated = proposal.model_copy(
        update={"status": "validated", "selected_candidate_id": "price-only"}
    )
    assert validated.selected_candidate_id == "price-only"
    assert validated.content_digest != proposal.content_digest


def test_validation_report_blocks_activation_on_error() -> None:
    proposal = _proposal().model_copy(
        update={"status": "validated", "selected_candidate_id": "price-only"}
    )
    report = MetricValidationReport(
        report_id="report-gmv",
        tenant_id="tenant",
        proposal_id=proposal.proposal_id,
        proposal_revision=proposal.revision,
        proposal_digest=proposal.content_digest,
        candidate_id="price-only",
        definition_digest=semantic_digest(_definition()),
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint="sha256:schema",
        binding_id="olist-binding",
        binding_version=2,
        validator_digest="sha256:validator",
        issues=(
            MetricValidationIssue(
                severity=MetricValidationSeverity.ERROR,
                code="FANOUT_DETECTED",
                message="Payment rows multiply item rows",
                field_refs=("orders.price",),
            ),
        ),
    )

    assert report.activation_allowed is False
    assert report.digest.startswith("sha256:")


def test_overlay_is_scoped_expires_and_pins_proposal_and_validation() -> None:
    now = datetime.now(UTC)
    proposal = _proposal()
    overlay = MetricOverlay(
        overlay_id="overlay-gmv",
        tenant_id="tenant",
        user_id="analyst@example.com",
        scope="run",
        run_id="run-123",
        base_context=_context(),
        proposal_id=proposal.proposal_id,
        proposal_revision=proposal.revision,
        proposal_digest=proposal.content_digest,
        definition=_definition(),
        validation_report_id="report-gmv",
        validation_report_digest="sha256:report",
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )

    assert overlay.content_digest.startswith("sha256:")
    with pytest.raises(ValidationError, match="run overlays require only run_id"):
        overlay.model_copy(update={"conversation_id": "conversation-1"})


def test_conversation_pin_and_domain_assignment_enforce_tenant_and_domain() -> None:
    with pytest.raises(ValidationError, match="tenant must match context"):
        ConversationMetricPin(
            tenant_id="other-tenant",
            user_id="analyst@example.com",
            conversation_id="conversation-1",
            domain_id="commerce",
            context=_context(),
        )

    pack = DomainPackIdentity(
        pack_id="commerce-core",
        version="1.0.0",
        digest="sha256:pack",
        domain_id="commerce",
    )
    assignment = DomainPackAssignment(
        tenant_id="tenant",
        source_id="olist",
        domain_id="commerce",
        pack=pack,
        assigned_by="steward@example.com",
    )
    assert assignment.pack == pack
    with pytest.raises(ValidationError, match="domain must match pack"):
        assignment.model_copy(update={"domain_id": "finance"})
