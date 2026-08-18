from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from data_agent.datasources import (
    DataSourceDefinition,
    DataSourceSnapshot,
    InMemoryDataSourceRegistry,
    SQLiteDataSourceRegistry,
    SemanticBindingRecord,
    SemanticFieldMapping,
)
from data_agent.datasources.registry import (
    DataSourceRegistryError,
    DataSourceRegistryErrorCode,
)
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
    MetricSetIdentity,
    MetricSetRecord,
    MetricValidationReport,
    SemanticAuditEvent,
    SemanticMetricDefinitionV2,
    semantic_digest,
)
from data_agent.tools.schemas import CatalogColumn, CatalogRelation, CatalogSnapshot


FINGERPRINT = "sha256:olist-schema"


def _metric(metric_ref: str, field_ref: str) -> SemanticMetricDefinitionV2:
    return SemanticMetricDefinitionV2(
        metric_ref=metric_ref,
        display_name=metric_ref.rsplit(".", 1)[-1].upper(),
        description=f"Governed {metric_ref}",
        formula=MetricAggregateFormula(
            operation="sum",
            operand=MetricFieldExpression(ref=field_ref),
        ),
        default_time_ref="orders.purchased_at",
        entity_key_refs=("orders.order_id",),
        currency="BRL",
    )


async def _seed_registry(registry):
    await registry.create(
        DataSourceDefinition(
            source_id="olist",
            tenant_id="tenant",
            name="OList",
            kind="csv",
            location_ref="internal://olist",
        )
    )
    catalog = CatalogSnapshot(
        schema_fingerprint=FINGERPRINT,
        relations=(
            CatalogRelation(
                relation="main.orders",
                columns=(
                    CatalogColumn(name="order_id", data_type="VARCHAR", nullable=False),
                    CatalogColumn(name="purchased_at", data_type="TIMESTAMP", nullable=False),
                    CatalogColumn(name="price", data_type="DOUBLE", nullable=False),
                    CatalogColumn(name="payment_value", data_type="DOUBLE", nullable=False),
                ),
            ),
        ),
    )
    await registry.publish_snapshot(
        DataSourceSnapshot(
            snapshot_id="olist-v1",
            tenant_id="tenant",
            source_id="olist",
            version=1,
            fingerprint=FINGERPRINT,
            catalog=catalog,
        )
    )
    binding = SemanticBindingRecord(
        binding_id="olist-binding",
        tenant_id="tenant",
        source_id="olist",
        source_snapshot_version=1,
        domain_id="commerce",
        version=1,
        mappings=tuple(
            SemanticFieldMapping(
                logical_ref=f"orders.{name}",
                physical_relation="main.orders",
                physical_column=name,
            )
            for name in ("order_id", "purchased_at", "price", "payment_value")
        ),
    )
    await registry.save_binding(binding)
    await registry.activate_binding(tenant_id="tenant", binding_id="olist-binding")
    return registry


async def _registry() -> InMemoryDataSourceRegistry:
    return await _seed_registry(InMemoryDataSourceRegistry())


async def _sqlite_registry(path: Path) -> SQLiteDataSourceRegistry:
    return await _seed_registry(SQLiteDataSourceRegistry(path))


def _proposal() -> MetricProposal:
    definition = _metric("commerce.gmv", "orders.price")
    return MetricProposal(
        proposal_id="proposal-gmv",
        tenant_id="tenant",
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint=FINGERPRINT,
        domain_id="commerce",
        base_binding_id="olist-binding",
        base_binding_version=1,
        requested_term="GMV",
        candidates=(
            MetricProposalCandidate(
                candidate_id="price-gmv",
                definition=definition,
                label="Item price GMV",
                rationale="Uses the item price field",
            ),
        ),
        created_by="analyst@example.com",
    )


def test_metric_governance_registry_enforces_revisions_and_authority() -> None:
    asyncio.run(_test_metric_governance_registry_enforces_revisions_and_authority())


async def _test_metric_governance_registry_enforces_revisions_and_authority() -> None:
    registry = await _registry()
    proposal = await registry.save_metric_proposal(_proposal())
    selected = proposal.model_copy(
        update={
            "revision": 2,
            "status": "validated",
            "selected_candidate_id": "price-gmv",
            "updated_at": datetime.now(UTC),
        }
    )
    await registry.update_metric_proposal(selected, expected_revision=1)
    with pytest.raises(DataSourceRegistryError) as stale:
        await registry.update_metric_proposal(selected, expected_revision=1)
    assert stale.value.code == DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT

    report = MetricValidationReport(
        report_id="report-gmv",
        tenant_id="tenant",
        proposal_id=selected.proposal_id,
        proposal_revision=selected.revision,
        proposal_digest=selected.content_digest,
        candidate_id="price-gmv",
        definition_digest=semantic_digest(selected.candidates[0].definition),
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint=FINGERPRINT,
        binding_id="olist-binding",
        binding_version=1,
        validator_digest="sha256:validator-v1",
    )
    await registry.save_metric_validation_report(report)

    metric_set = MetricSetRecord.create(
        tenant_id="tenant",
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint=FINGERPRINT,
        domain_id="commerce",
        binding_id="olist-binding",
        binding_version=1,
        metric_set_id="commerce-metrics",
        version=1,
        definitions=(_metric("commerce.revenue", "orders.payment_value"),),
        created_by="steward@example.com",
    )
    await registry.save_metric_set(metric_set)
    pointer = await registry.activate_metric_set(
        tenant_id="tenant",
        metric_set_id="commerce-metrics",
        version=1,
        expected_pointer_revision=0,
    )
    assert pointer.revision == 1
    assert (await registry.get_metric_set(
        tenant_id="tenant", metric_set_id="commerce-metrics", version=1
    )).status == "published"

    with pytest.raises(DataSourceRegistryError) as conflict:
        await registry.activate_metric_set(
            tenant_id="tenant",
            metric_set_id="commerce-metrics",
            version=1,
            expected_pointer_revision=0,
        )
    assert conflict.value.code == DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT


def test_overlay_pin_domain_assignment_audit_and_delete_cleanup() -> None:
    asyncio.run(_test_overlay_pin_domain_assignment_audit_and_delete_cleanup())


async def _test_overlay_pin_domain_assignment_audit_and_delete_cleanup() -> None:
    registry = await _registry()
    proposal = await registry.save_metric_proposal(_proposal())
    proposal = proposal.model_copy(
        update={
            "revision": 2,
            "status": "validated",
            "selected_candidate_id": "price-gmv",
            "updated_at": datetime.now(UTC),
        }
    )
    await registry.update_metric_proposal(proposal, expected_revision=1)
    report = MetricValidationReport(
        report_id="report-gmv",
        tenant_id="tenant",
        proposal_id=proposal.proposal_id,
        proposal_revision=proposal.revision,
        proposal_digest=proposal.content_digest,
        candidate_id="price-gmv",
        definition_digest=semantic_digest(proposal.candidates[0].definition),
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint=FINGERPRINT,
        binding_id="olist-binding",
        binding_version=1,
        validator_digest="sha256:validator-v1",
    )
    await registry.save_metric_validation_report(report)

    revenue_set = MetricSetRecord.create(
        tenant_id="tenant",
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint=FINGERPRINT,
        domain_id="commerce",
        binding_id="olist-binding",
        binding_version=1,
        metric_set_id="commerce-metrics",
        version=1,
        definitions=(_metric("commerce.revenue", "orders.payment_value"),),
        created_by="steward@example.com",
    )
    await registry.save_metric_set(revenue_set)
    pointer = await registry.activate_metric_set(
        tenant_id="tenant",
        metric_set_id="commerce-metrics",
        version=1,
        expected_pointer_revision=0,
    )
    base_context = MetricContextPin(
        tenant_id="tenant",
        source_id="olist",
        source_version=1,
        binding_id="olist-binding",
        binding_version=1,
        metric_set=MetricSetIdentity(
            metric_set_id=pointer.metric_set_id,
            version=pointer.metric_set_version,
            digest=pointer.metric_set_digest,
        ),
    )
    now = datetime.now(UTC)
    overlay = MetricOverlay(
        overlay_id="overlay-gmv",
        tenant_id="tenant",
        user_id="analyst@example.com",
        scope="conversation",
        conversation_id="conversation-1",
        base_context=base_context,
        proposal_id=proposal.proposal_id,
        proposal_revision=proposal.revision,
        proposal_digest=proposal.content_digest,
        definition=proposal.candidates[0].definition,
        validation_report_id=report.report_id,
        validation_report_digest=report.digest,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    await registry.save_metric_overlay(overlay)
    run_overlay = overlay.model_copy(
        update={
            "overlay_id": "overlay-run-gmv",
            "scope": "run",
            "run_id": "run-sqlite",
            "conversation_id": None,
        }
    )
    await registry.save_metric_overlay(run_overlay)
    pinned_context = base_context.model_copy(
        update={
            "overlay_id": overlay.overlay_id,
            "overlay_digest": overlay.content_digest,
        }
    )
    pin = ConversationMetricPin(
        tenant_id="tenant",
        user_id="analyst@example.com",
        conversation_id="conversation-1",
        domain_id="commerce",
        context=pinned_context,
    )
    await registry.put_conversation_metric_pin(pin, expected_revision=0)
    assert (await registry.get_conversation_metric_pin(
        tenant_id="tenant",
        user_id="analyst@example.com",
        conversation_id="conversation-1",
    )) == pin

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
    await registry.put_domain_pack_assignment(assignment, expected_revision=0)
    event = SemanticAuditEvent(
        event_id="audit-1",
        tenant_id="tenant",
        source_id="olist",
        actor_id="steward@example.com",
        action="metric.overlay.confirmed",
        resource_type="metric_overlay",
        resource_id=overlay.overlay_id,
    )
    await registry.append_semantic_audit_event(event)
    assert await registry.list_semantic_audit_events(
        tenant_id="tenant",
        resource_type="metric_overlay",
        resource_id=overlay.overlay_id,
    ) == (event,)

    await registry.delete(tenant_id="tenant", source_id="olist")
    assert await registry.get_metric_proposal(
        tenant_id="tenant", proposal_id=proposal.proposal_id
    ) is None
    assert await registry.get_metric_overlay(
        tenant_id="tenant", overlay_id=overlay.overlay_id
    ) is None
    assert await registry.get_conversation_metric_pin(
        tenant_id="tenant",
        user_id="analyst@example.com",
        conversation_id="conversation-1",
    ) is None


def test_sqlite_registry_migrates_and_persists_metric_control_plane(tmp_path: Path) -> None:
    database_path = tmp_path / "control-plane.sqlite3"
    asyncio.run(_test_sqlite_registry_migrates_and_persists(database_path))

    with sqlite3.connect(database_path) as connection:
        migrations = connection.execute(
            "SELECT version FROM control_schema_migrations ORDER BY version"
        ).fetchall()
        assert migrations == [(1,), (2,)]


async def _test_sqlite_registry_migrates_and_persists(database_path: Path) -> None:
    registry = await _sqlite_registry(database_path)
    proposal = await registry.save_metric_proposal(_proposal())
    proposal = proposal.model_copy(
        update={
            "revision": 2,
            "status": "validated",
            "selected_candidate_id": "price-gmv",
            "updated_at": datetime.now(UTC),
        }
    )
    await registry.update_metric_proposal(proposal, expected_revision=1)
    report = MetricValidationReport(
        report_id="report-gmv",
        tenant_id="tenant",
        proposal_id=proposal.proposal_id,
        proposal_revision=proposal.revision,
        proposal_digest=proposal.content_digest,
        candidate_id="price-gmv",
        definition_digest=semantic_digest(proposal.candidates[0].definition),
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint=FINGERPRINT,
        binding_id="olist-binding",
        binding_version=1,
        validator_digest="sha256:validator-v1",
    )
    await registry.save_metric_validation_report(report)
    metric_set = MetricSetRecord.create(
        tenant_id="tenant",
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint=FINGERPRINT,
        domain_id="commerce",
        binding_id="olist-binding",
        binding_version=1,
        metric_set_id="commerce-metrics",
        version=1,
        definitions=(_metric("commerce.revenue", "orders.payment_value"),),
        created_by="steward@example.com",
    )
    await registry.save_metric_set(metric_set)
    pointer = await registry.activate_metric_set(
        tenant_id="tenant",
        metric_set_id="commerce-metrics",
        version=1,
        expected_pointer_revision=0,
    )
    base_context = MetricContextPin(
        tenant_id="tenant",
        source_id="olist",
        source_version=1,
        binding_id="olist-binding",
        binding_version=1,
        metric_set=MetricSetIdentity(
            metric_set_id=pointer.metric_set_id,
            version=pointer.metric_set_version,
            digest=pointer.metric_set_digest,
        ),
    )
    now = datetime.now(UTC)
    overlay = MetricOverlay(
        overlay_id="overlay-gmv",
        tenant_id="tenant",
        user_id="analyst@example.com",
        scope="conversation",
        conversation_id="conversation-sqlite",
        base_context=base_context,
        proposal_id=proposal.proposal_id,
        proposal_revision=proposal.revision,
        proposal_digest=proposal.content_digest,
        definition=proposal.candidates[0].definition,
        validation_report_id=report.report_id,
        validation_report_digest=report.digest,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    await registry.save_metric_overlay(overlay)
    run_overlay = overlay.model_copy(
        update={
            "overlay_id": "overlay-run-gmv",
            "scope": "run",
            "run_id": "run-sqlite",
            "conversation_id": None,
        }
    )
    await registry.save_metric_overlay(run_overlay)
    pin = ConversationMetricPin(
        tenant_id="tenant",
        user_id="analyst@example.com",
        conversation_id="conversation-sqlite",
        domain_id="commerce",
        context=base_context.model_copy(
            update={
                "overlay_id": overlay.overlay_id,
                "overlay_digest": overlay.content_digest,
            }
        ),
    )
    await registry.put_conversation_metric_pin(pin, expected_revision=0)
    assignment = DomainPackAssignment(
        tenant_id="tenant",
        source_id="olist",
        domain_id="commerce",
        pack=DomainPackIdentity(
            pack_id="commerce-core",
            version="1.0.0",
            digest="sha256:pack",
            domain_id="commerce",
        ),
        assigned_by="steward@example.com",
    )
    await registry.put_domain_pack_assignment(assignment, expected_revision=0)
    event = SemanticAuditEvent(
        event_id="audit-sqlite",
        tenant_id="tenant",
        source_id="olist",
        actor_id="steward@example.com",
        action="metric_set.activated",
        resource_type="metric_set",
        resource_id="commerce-metrics:1",
    )
    await registry.append_semantic_audit_event(event)

    reopened = SQLiteDataSourceRegistry(database_path)
    assert await reopened.get_metric_proposal(
        tenant_id="tenant", proposal_id=proposal.proposal_id
    ) == proposal
    assert await reopened.get_metric_validation_report(
        tenant_id="tenant", report_id=report.report_id
    ) == report
    assert await reopened.get_active_metric_set(
        tenant_id="tenant", source_id="olist", domain_id="commerce"
    ) == pointer
    assert await reopened.get_metric_overlay(
        tenant_id="tenant", overlay_id=overlay.overlay_id
    ) == overlay
    assert await reopened.get_run_metric_overlay(
        tenant_id="tenant",
        user_id="analyst@example.com",
        run_id="run-sqlite",
    ) == run_overlay
    assert await reopened.get_conversation_metric_pin(
        tenant_id="tenant",
        user_id="analyst@example.com",
        conversation_id="conversation-sqlite",
    ) == pin
    assert await reopened.get_domain_pack_assignment(
        tenant_id="tenant", source_id="olist", domain_id="commerce"
    ) == assignment
    assert await reopened.list_semantic_audit_events(
        tenant_id="tenant",
        resource_type="metric_set",
        resource_id="commerce-metrics:1",
    ) == (event,)

    await reopened.delete(tenant_id="tenant", source_id="olist")
    assert await reopened.get_metric_set(
        tenant_id="tenant", metric_set_id="commerce-metrics", version=1
    ) is None
    assert await reopened.get_metric_overlay(
        tenant_id="tenant", overlay_id=overlay.overlay_id
    ) is None
    assert await reopened.get_run_metric_overlay(
        tenant_id="tenant",
        user_id="analyst@example.com",
        run_id="run-sqlite",
    ) is None
