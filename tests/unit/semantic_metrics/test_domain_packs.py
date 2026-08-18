from __future__ import annotations

import pytest

from data_agent.datasources import SemanticBindingRecord, SemanticFieldMapping
from data_agent.semantic_metrics import (
    COMMERCE_PACK,
    DomainPackRegistry,
    MetricAggregateFormula,
    MetricBinaryExpression,
)
from data_agent.tools.schemas import CatalogColumn, CatalogRelation, CatalogSnapshot


def _binding() -> SemanticBindingRecord:
    return SemanticBindingRecord(
        binding_id="olist",
        tenant_id="tenant",
        source_id="olist",
        source_snapshot_version=1,
        domain_id="commerce",
        version=1,
        status="active",
        mappings=tuple(
            SemanticFieldMapping(
                logical_ref=f"olist.{name}",
                physical_relation="main.olist",
                physical_column=name,
                semantic_role=("time" if "timestamp" in name else "measure"),
            )
            for name in (
                "price",
                "freight_value",
                "payment_value",
                "order_purchase_timestamp",
            )
        ),
    )


def _catalog() -> CatalogSnapshot:
    return CatalogSnapshot(
        schema_fingerprint="sha256:olist",
        relations=(
            CatalogRelation(
                relation="main.olist",
                columns=(
                    CatalogColumn(name="price", data_type="DOUBLE", nullable=False),
                    CatalogColumn(
                        name="freight_value", data_type="DOUBLE", nullable=False
                    ),
                    CatalogColumn(
                        name="payment_value", data_type="DOUBLE", nullable=False
                    ),
                    CatalogColumn(
                        name="order_purchase_timestamp",
                        data_type="TIMESTAMP",
                        nullable=False,
                    ),
                ),
            ),
        ),
    )


def test_commerce_pack_detects_multilingual_gmv_terms() -> None:
    registry = DomainPackRegistry()

    english = registry.detect_templates("Show quarterly GMV", domain_id="commerce")
    chinese = registry.detect_templates("查询季度成交总额", domain_id="commerce")

    assert english[0][0].digest == COMMERCE_PACK.digest
    assert english[0][1].metric_ref == "commerce.gmv"
    assert chinese[0][2] == "成交总额"
    assert registry.detect_templates("gmvalue", domain_id="commerce") == ()


def test_domain_pack_digest_rejects_content_tampering() -> None:
    payload = COMMERCE_PACK.model_dump(mode="json")
    payload["description"] = "tampered"

    with pytest.raises(ValueError, match="digest"):
        type(COMMERCE_PACK).model_validate(payload)


def test_olist_grounding_generates_three_explicit_gmv_variants() -> None:
    candidates = DomainPackRegistry().propose(
        requested_term="GMV",
        binding=_binding(),
        catalog=_catalog(),
        domain_id="commerce",
    )

    assert [item.candidate_id for item in candidates] == [
        "gmv-item-price",
        "gmv-price-plus-freight",
        "gmv-payment-value",
    ]
    compound = candidates[1].definition.formula
    assert isinstance(compound, MetricAggregateFormula)
    assert isinstance(compound.operand, MetricBinaryExpression)
    assert candidates[1].definition.scope.includes_freight is True
    assert "退款处理" in candidates[1].required_decisions
    assert candidates[0].definition.default_time_ref == (
        "olist.order_purchase_timestamp"
    )
    assert candidates[0].definition.provenance[0].digest == COMMERCE_PACK.digest
