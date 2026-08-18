from __future__ import annotations

import asyncio
from tempfile import TemporaryDirectory

import pytest

from data_agent.datasources import (
    DataSourceDefinition,
    DataSourceSnapshot,
    InMemoryDataSourceRegistry,
    SemanticBindingRecord,
    SemanticFieldMapping,
)
from data_agent.semantic_metrics import (
    MetricAggregateFormula,
    MetricFieldExpression,
    MetricProposalCandidate,
    MetricSetRecord,
    SemanticMetricActor,
    SemanticMetricDefinitionV2,
    SemanticMetricGovernanceService,
    SemanticMetricFeatures,
    SemanticMetricServiceError,
    SemanticMetricServiceErrorCode,
)
from api.datasource_service import DataSourceService
from data_agent.tools.schemas import CatalogColumn, CatalogRelation, CatalogSnapshot


async def _service():
    registry = InMemoryDataSourceRegistry()
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
        schema_fingerprint="sha256:service",
        relations=(
            CatalogRelation(
                relation="main.items",
                columns=(
                    CatalogColumn(name="price", data_type="DOUBLE", nullable=False),
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
            fingerprint="sha256:service",
            catalog=catalog,
        )
    )
    await registry.save_binding(
        SemanticBindingRecord(
            binding_id="olist-binding",
            tenant_id="tenant",
            source_id="olist",
            source_snapshot_version=1,
            domain_id="commerce",
            version=1,
            mappings=(
                SemanticFieldMapping(
                    logical_ref="item.price",
                    physical_relation="main.items",
                    physical_column="price",
                    semantic_role="measure",
                ),
            ),
        )
    )
    data_sources = DataSourceService(registry=registry)
    await data_sources.activate_binding(
        tenant_id="tenant",
        source_id="olist",
        binding_id="olist-binding",
        assigned_by="steward",
    )
    await data_sources.close()
    return registry, SemanticMetricGovernanceService(
        registry,
        features=SemanticMetricFeatures(provisional_overlays=True),
    )


def _candidate() -> MetricProposalCandidate:
    return MetricProposalCandidate(
        candidate_id="gmv-price",
        label="Price GMV",
        rationale="Uses governed item price",
        definition=SemanticMetricDefinitionV2(
            metric_ref="commerce.gmv",
            display_name="GMV",
            description="Sum of item price",
            synonyms=("成交总额",),
            formula=MetricAggregateFormula(
                operation="sum",
                operand=MetricFieldExpression(ref="item.price"),
            ),
            unit="currency",
            currency="BRL",
        ),
    )


def test_governed_lifecycle_creates_overlay_and_activates_versioned_set() -> None:
    asyncio.run(_test_governed_lifecycle())


def test_agent_discovery_reuses_open_proposal_for_same_authority() -> None:
    async def scenario() -> None:
        registry, service = await _service()
        actor = SemanticMetricActor(
            tenant_id="tenant", user_id="analyst", roles=("analyst",)
        )
        first = await service.discover_or_reuse_proposal(
            actor=actor,
            source_id="olist",
            domain_id="commerce",
            requested_term="GMV",
        )
        second = await service.discover_or_reuse_proposal(
            actor=actor,
            source_id="olist",
            domain_id="commerce",
            requested_term="gmv",
        )

        assert first.proposal_id == second.proposal_id
        assert len(await registry.list_metric_proposals(
            tenant_id="tenant", source_id="olist"
        )) == 1

    asyncio.run(scenario())


async def _test_governed_lifecycle() -> None:
    registry, service = await _service()
    analyst = SemanticMetricActor(
        tenant_id="tenant", user_id="analyst", roles=("analyst",)
    )
    validator = SemanticMetricActor(
        tenant_id="tenant", user_id="steward", roles=("semantic_editor",)
    )
    approver = SemanticMetricActor(
        tenant_id="tenant", user_id="admin", roles=("semantic_admin",)
    )

    proposal = await service.create_proposal(
        actor=analyst,
        source_id="olist",
        domain_id="commerce",
        requested_term="GMV",
        candidates=(_candidate(),),
    )
    selected = await service.select_candidate(
        actor=analyst,
        proposal_id=proposal.proposal_id,
        candidate_id="gmv-price",
        expected_revision=1,
    )
    validated, report = await service.validate_proposal(
        actor=validator,
        proposal_id=proposal.proposal_id,
        expected_revision=selected.revision,
    )
    overlay = await service.create_overlay(
        actor=analyst,
        proposal_id=proposal.proposal_id,
        validation_report_id=report.report_id,
        scope="conversation",
        conversation_id="conversation-1",
    )
    run_overlay = await service.create_overlay(
        actor=analyst,
        proposal_id=proposal.proposal_id,
        validation_report_id=report.report_id,
        scope="run",
        run_id="run-preview-1",
    )
    approved, metric_set, pointer = await service.approve_and_activate(
        actor=approver,
        proposal_id=proposal.proposal_id,
        validation_report_id=report.report_id,
        expected_revision=validated.revision,
        expected_pointer_revision=0,
    )

    assert validated.status == "pending_approval"
    assert overlay.definition.metric_ref == "commerce.gmv"
    conversation_pin = await registry.get_conversation_metric_pin(
        tenant_id="tenant",
        user_id="analyst",
        conversation_id="conversation-1",
    )
    assert conversation_pin is not None
    assert conversation_pin.context.overlay_id == overlay.overlay_id
    assert conversation_pin.context.overlay_digest == overlay.content_digest
    assert approved.status == "approved"
    assert metric_set.version == 1
    assert pointer.metric_set_digest == metric_set.content_digest
    binding = (await registry.list_bindings(
        tenant_id="tenant", source_id="olist"
    ))[0]
    with TemporaryDirectory() as temporary:
        data_sources = DataSourceService(state_root=temporary, registry=registry)
        resolved_metrics = await data_sources.resolve_metric_context(
            tenant_id="tenant",
            user_id="analyst",
            conversation_id="conversation-1",
            source_id="olist",
            domain_id="commerce",
            binding=binding,
        )
        match = resolved_metrics.catalog.resolve("GMV")
        assert match.matches[0].origin == "overlay"
        assert resolved_metrics.domain_pack is not None
        assert resolved_metrics.domain_pack.pack_id == "domain.commerce"
        resolved_run_metrics = await data_sources.resolve_metric_context(
            tenant_id="tenant",
            user_id="analyst",
            conversation_id=None,
            run_id="run-preview-1",
            source_id="olist",
            domain_id="commerce",
            binding=binding,
        )
        run_match = resolved_run_metrics.catalog.resolve("GMV")
        assert run_match.matches[0].origin == "overlay"
        assert resolved_run_metrics.context.overlay_id == run_overlay.overlay_id
        await data_sources.close()
    assert (await registry.get_metric_set(
        tenant_id="tenant",
        metric_set_id=metric_set.metric_set_id,
        version=1,
    )).status == "published"
    audit = await registry.list_semantic_audit_events(
        tenant_id="tenant",
        resource_type="metricsetrecord",
        resource_id=metric_set.metric_set_id,
    )
    assert len(audit) == 1
    assert audit[0].actor_id == "admin"


def test_rbac_and_required_decisions_cannot_be_bypassed() -> None:
    asyncio.run(_test_rbac_and_required_decisions())


async def _test_rbac_and_required_decisions() -> None:
    _, service = await _service()
    viewer = SemanticMetricActor(
        tenant_id="tenant", user_id="viewer", roles=("viewer",)
    )
    analyst = SemanticMetricActor(
        tenant_id="tenant", user_id="analyst", roles=("analyst",)
    )
    with pytest.raises(SemanticMetricServiceError) as denied:
        await service.create_proposal(
            actor=viewer,
            source_id="olist",
            domain_id="commerce",
            requested_term="GMV",
            candidates=(_candidate(),),
        )
    assert denied.value.code == SemanticMetricServiceErrorCode.PERMISSION_DENIED

    candidate = _candidate().model_copy(
        update={"required_decisions": ("Include freight?",)}
    )
    proposal = await service.create_proposal(
        actor=analyst,
        source_id="olist",
        domain_id="commerce",
        requested_term="GMV",
        candidates=(candidate,),
    )
    selected = await service.select_candidate(
        actor=analyst,
        proposal_id=proposal.proposal_id,
        candidate_id=candidate.candidate_id,
        expected_revision=1,
    )
    with pytest.raises(SemanticMetricServiceError) as unresolved:
        await service.validate_proposal(
            actor=SemanticMetricActor(
                tenant_id="tenant", user_id="steward", roles=("semantic_editor",)
            ),
            proposal_id=proposal.proposal_id,
            expected_revision=selected.revision,
        )
    assert unresolved.value.code == SemanticMetricServiceErrorCode.INVALID_STATE


def test_conversation_metric_pin_keeps_historical_metric_set() -> None:
    asyncio.run(_test_conversation_metric_pin_keeps_historical_metric_set())


async def _test_conversation_metric_pin_keeps_historical_metric_set() -> None:
    registry, _ = await _service()
    first = MetricSetRecord.create(
        tenant_id="tenant",
        source_id="olist",
        source_snapshot_version=1,
        schema_fingerprint="sha256:service",
        domain_id="commerce",
        binding_id="olist-binding",
        binding_version=1,
        metric_set_id="commerce-metrics",
        version=1,
        definitions=(_candidate().definition,),
        created_by="admin",
    )
    await registry.save_metric_set(first)
    await registry.activate_metric_set(
        tenant_id="tenant",
        metric_set_id=first.metric_set_id,
        version=1,
        expected_pointer_revision=0,
    )
    binding = next(
        item
        for item in await registry.list_bindings(
            tenant_id="tenant", source_id="olist"
        )
        if item.status == "active"
    )
    with TemporaryDirectory() as temporary:
        data_sources = DataSourceService(state_root=temporary, registry=registry)
        pinned = await data_sources.resolve_metric_context(
            tenant_id="tenant",
            user_id="analyst",
            conversation_id="conversation-1",
            source_id="olist",
            domain_id="commerce",
            binding=binding,
        )
        second_definition = _candidate().definition.model_copy(
            update={
                "metric_ref": "commerce.item_sales",
                "display_name": "Item sales",
                "synonyms": (),
            }
        )
        second = MetricSetRecord.create(
            tenant_id="tenant",
            source_id="olist",
            source_snapshot_version=1,
            schema_fingerprint="sha256:service",
            domain_id="commerce",
            binding_id="olist-binding",
            binding_version=1,
            metric_set_id="commerce-metrics",
            version=2,
            definitions=(_candidate().definition, second_definition),
            created_by="admin",
        )
        await registry.save_metric_set(second)
        await registry.activate_metric_set(
            tenant_id="tenant",
            metric_set_id=second.metric_set_id,
            version=2,
            expected_pointer_revision=1,
        )
        historical = await data_sources.resolve_metric_context(
            tenant_id="tenant",
            user_id="analyst",
            conversation_id="conversation-1",
            source_id="olist",
            domain_id="commerce",
            binding=binding,
        )
        current = await data_sources.resolve_metric_context(
            tenant_id="tenant",
            user_id="analyst",
            conversation_id="conversation-2",
            source_id="olist",
            domain_id="commerce",
            binding=binding,
        )
        await data_sources.close()

    assert pinned.context.metric_set.version == 1
    assert historical.context.metric_set.version == 1
    assert {item.definition.metric_ref for item in historical.catalog.entries} == {
        "commerce.gmv"
    }
    assert current.context.metric_set.version == 2
    assert {item.definition.metric_ref for item in current.catalog.entries} == {
        "commerce.gmv",
        "commerce.item_sales",
    }
