"""Versioned domain knowledge packs and schema-grounded metric proposals."""

from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from data_agent.datasources.models import (
    SemanticBindingRecord,
    SemanticGraphBindingRecord,
)
from data_agent.tools.schemas import CatalogSnapshot

from .ast import (
    MetricAggregateFormula,
    MetricBinaryExpression,
    MetricFieldExpression,
)
from .digest import semantic_digest
from .models import (
    MetricAstModel,
    MetricProposalCandidate,
    MetricProvenance,
    MetricProvenanceKind,
    MetricRiskTier,
    MetricScopeConvention,
    SemanticMetricDefinitionV2,
)
from .types import NonBlankText, StableIdentifier


class DomainMetricTemplate(MetricAstModel):
    template_id: StableIdentifier
    metric_ref: NonBlankText
    display_name: NonBlankText
    terms: tuple[NonBlankText, ...] = Field(min_length=1)
    description: NonBlankText
    risk_tier: MetricRiskTier = MetricRiskTier.HIGH
    required_decisions: tuple[NonBlankText, ...] = ()

    @field_validator("terms", "required_decisions")
    @classmethod
    def unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len({value.casefold() for value in values}):
            raise ValueError("domain template values must be unique")
        return values


class DomainPackManifest(MetricAstModel):
    pack_id: StableIdentifier
    version: NonBlankText
    domain_id: StableIdentifier
    display_name: NonBlankText
    description: NonBlankText
    analysis_skill_id: StableIdentifier
    analysis_skill_version: NonBlankText
    templates: tuple[DomainMetricTemplate, ...]
    digest: NonBlankText

    @model_validator(mode="after")
    def validate_digest(self) -> "DomainPackManifest":
        expected = semantic_digest(
            {
                "pack_id": self.pack_id,
                "version": self.version,
                "domain_id": self.domain_id,
                "display_name": self.display_name,
                "description": self.description,
                "analysis_skill_id": self.analysis_skill_id,
                "analysis_skill_version": self.analysis_skill_version,
                "templates": self.templates,
            }
        )
        if self.digest != expected:
            raise ValueError("domain pack digest does not match its immutable content")
        return self

    @classmethod
    def create(
        cls,
        *,
        pack_id: str,
        version: str,
        domain_id: str,
        display_name: str,
        description: str,
        analysis_skill_id: str,
        analysis_skill_version: str,
        templates: tuple[DomainMetricTemplate, ...],
    ) -> "DomainPackManifest":
        digest = semantic_digest(
            {
                "pack_id": pack_id,
                "version": version,
                "domain_id": domain_id,
                "display_name": display_name,
                "description": description,
                "analysis_skill_id": analysis_skill_id,
                "analysis_skill_version": analysis_skill_version,
                "templates": templates,
            }
        )
        return cls(
            pack_id=pack_id,
            version=version,
            domain_id=domain_id,
            display_name=display_name,
            description=description,
            analysis_skill_id=analysis_skill_id,
            analysis_skill_version=analysis_skill_version,
            templates=templates,
            digest=digest,
        )


GMV_TEMPLATE = DomainMetricTemplate(
    template_id="commerce.gmv",
    metric_ref="commerce.gmv",
    display_name="GMV",
    terms=("GMV", "Gross Merchandise Value", "成交总额", "商品交易总额"),
    description="Gross merchandise value before enterprise-specific scope decisions.",
    risk_tier=MetricRiskTier.HIGH,
    required_decisions=("订单状态范围", "季度时间字段", "退款处理", "币种"),
)

COMMERCE_PACK = DomainPackManifest.create(
    pack_id="domain.commerce",
    version="1.0.0",
    domain_id="commerce",
    display_name="Commerce",
    description="Commerce vocabulary, metric templates, and deterministic checks.",
    analysis_skill_id="dataset.analytics.commerce",
    analysis_skill_version="1.0.0",
    templates=(GMV_TEMPLATE,),
)

FINANCE_PACK = DomainPackManifest.create(
    pack_id="domain.finance",
    version="1.0.0",
    domain_id="finance",
    display_name="Finance",
    description="Finance vocabulary and governed accounting metric templates.",
    analysis_skill_id="dataset.analytics.finance",
    analysis_skill_version="1.0.0",
    templates=(
        DomainMetricTemplate(
            template_id="finance.revenue",
            metric_ref="finance.revenue",
            display_name="Revenue",
            terms=("revenue", "income", "营收", "收入", "净收入"),
            description="Revenue under an enterprise-approved accounting policy.",
            required_decisions=("收入确认规则", "退款与折让", "税费", "币种"),
        ),
        DomainMetricTemplate(
            template_id="finance.profit",
            metric_ref="finance.profit",
            display_name="Profit",
            terms=("profit", "margin", "利润", "毛利", "净利"),
            description="Profit under an enterprise-approved accounting policy.",
            required_decisions=("收入口径", "成本范围", "税费", "币种"),
        ),
    ),
)


class DomainPackRegistry:
    def __init__(
        self,
        manifests: tuple[DomainPackManifest, ...] = (COMMERCE_PACK, FINANCE_PACK),
    ) -> None:
        keys = [(item.pack_id, item.version) for item in manifests]
        if len(keys) != len(set(keys)):
            raise ValueError("domain pack versions must be unique")
        self._manifests = tuple(manifests)

    @property
    def manifests(self) -> tuple[DomainPackManifest, ...]:
        return self._manifests

    def get(self, pack_id: str, version: str) -> DomainPackManifest | None:
        return next(
            (
                item
                for item in self._manifests
                if item.pack_id == pack_id and item.version == version
            ),
            None,
        )

    def for_domain(self, domain_id: str) -> tuple[DomainPackManifest, ...]:
        return tuple(
            item for item in self._manifests if item.domain_id == domain_id
        )

    def detect_templates(
        self,
        question: str,
        *,
        domain_id: str | None = None,
    ) -> tuple[tuple[DomainPackManifest, DomainMetricTemplate, str], ...]:
        matches: list[tuple[DomainPackManifest, DomainMetricTemplate, str]] = []
        for manifest in self._manifests:
            if domain_id is not None and manifest.domain_id != domain_id:
                continue
            for template in manifest.templates:
                for term in template.terms:
                    if _contains_term(question, term):
                        matches.append((manifest, template, term))
                        break
        return tuple(matches)

    def propose(
        self,
        *,
        requested_term: str,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        catalog: CatalogSnapshot,
        domain_id: str,
    ) -> tuple[MetricProposalCandidate, ...]:
        matches = self.detect_templates(requested_term, domain_id=domain_id)
        if not matches:
            return ()
        manifest, template, _ = matches[0]
        if template.template_id != "commerce.gmv":
            return ()
        return _commerce_gmv_candidates(
            manifest=manifest,
            template=template,
            binding=binding,
            catalog=catalog,
        )


def _contains_term(text: str, term: str) -> bool:
    if term.isascii() and any(character.isalnum() for character in term):
        return re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
            text,
            re.IGNORECASE,
        ) is not None
    return term.casefold() in text.casefold()


def _commerce_gmv_candidates(
    *,
    manifest: DomainPackManifest,
    template: DomainMetricTemplate,
    binding: SemanticBindingRecord | SemanticGraphBindingRecord,
    catalog: CatalogSnapshot,
) -> tuple[MetricProposalCandidate, ...]:
    search = _field_search_text(binding, catalog)
    price = _best_ref(search, ("item_price", "price", "商品价格", "售价"))
    freight = _best_ref(
        search,
        ("freight_value", "freight", "shipping_fee", "运费"),
    )
    payment = _best_ref(
        search,
        ("payment_value", "paid_amount", "payment", "支付金额"),
    )
    time_ref = _best_ref(
        search,
        (
            "order_purchase_timestamp",
            "purchased_at",
            "purchase_time",
            "created_at",
            "下单时间",
        ),
    )
    provenance = (
        MetricProvenance(
            kind=MetricProvenanceKind.DOMAIN_PACK,
            reference=f"{manifest.pack_id}@{manifest.version}:{template.template_id}",
            digest=manifest.digest,
        ),
    )
    time_values = {
        "default_time_ref": time_ref,
        "allowed_time_refs": (time_ref,) if time_ref is not None else (),
    }
    common_decisions = [*template.required_decisions]
    if time_ref is not None:
        common_decisions.append(f"确认季度归属时间：{time_ref}")
    candidates: list[MetricProposalCandidate] = []
    if price is not None:
        candidates.append(
            MetricProposalCandidate(
                candidate_id="gmv-item-price",
                label="商品价格 GMV",
                rationale=f"按 {price} 汇总，不含运费。",
                required_decisions=tuple(dict.fromkeys(common_decisions)),
                definition=SemanticMetricDefinitionV2(
                    metric_ref=template.metric_ref,
                    display_name=template.display_name,
                    description="Gross merchandise value from item price.",
                    synonyms=template.terms,
                    formula=MetricAggregateFormula(
                        operation="sum",
                        operand=MetricFieldExpression(ref=price),
                    ),
                    scope=MetricScopeConvention(includes_freight=False),
                    unit="currency",
                    provenance=provenance,
                    **time_values,
                ),
            )
        )
    if price is not None and freight is not None:
        candidates.append(
            MetricProposalCandidate(
                candidate_id="gmv-price-plus-freight",
                label="商品价格加运费 GMV",
                rationale=f"按 {price} + {freight} 后汇总。",
                required_decisions=tuple(dict.fromkeys(common_decisions)),
                definition=SemanticMetricDefinitionV2(
                    metric_ref=template.metric_ref,
                    display_name=template.display_name,
                    description="Gross merchandise value from item price plus freight.",
                    synonyms=template.terms,
                    formula=MetricAggregateFormula(
                        operation="sum",
                        operand=MetricBinaryExpression(
                            operation="add",
                            left=MetricFieldExpression(ref=price),
                            right=MetricFieldExpression(ref=freight),
                        ),
                    ),
                    scope=MetricScopeConvention(includes_freight=True),
                    unit="currency",
                    provenance=provenance,
                    **time_values,
                ),
            )
        )
    if payment is not None:
        candidates.append(
            MetricProposalCandidate(
                candidate_id="gmv-payment-value",
                label="支付金额 GMV",
                rationale=f"按 {payment} 汇总；必须验证支付表粒度。",
                required_decisions=tuple(
                    dict.fromkeys((*common_decisions, "支付记录去重与分期处理"))
                ),
                definition=SemanticMetricDefinitionV2(
                    metric_ref=template.metric_ref,
                    display_name=template.display_name,
                    description="Gross merchandise value from payment value.",
                    synonyms=template.terms,
                    formula=MetricAggregateFormula(
                        operation="sum",
                        operand=MetricFieldExpression(ref=payment),
                    ),
                    unit="currency",
                    provenance=provenance,
                    **time_values,
                ),
            )
        )
    return tuple(candidates)


def _field_search_text(
    binding: SemanticBindingRecord | SemanticGraphBindingRecord,
    catalog: CatalogSnapshot,
) -> dict[str, str]:
    physical_by_id = {
        column.column_id: column.name
        for relation in catalog.relations
        for column in relation.columns
    }
    values: dict[str, str] = {}
    for mapping in binding.mappings:
        physical = (
            mapping.physical_column
            if isinstance(binding, SemanticBindingRecord)
            else physical_by_id.get(mapping.column_id, "")
        )
        values[mapping.logical_ref] = " ".join(
            str(value)
            for value in (
                mapping.logical_ref,
                physical,
                mapping.display_name or "",
                mapping.description or "",
                *mapping.synonyms,
            )
        ).casefold()
    return values


def _best_ref(search: dict[str, str], tokens: tuple[str, ...]) -> str | None:
    for token in tokens:
        normalized = token.casefold()
        matches = sorted(ref for ref, text in search.items() if normalized in text)
        if matches:
            return matches[0]
    return None


__all__ = [
    "COMMERCE_PACK",
    "FINANCE_PACK",
    "DomainMetricTemplate",
    "DomainPackManifest",
    "DomainPackRegistry",
    "GMV_TEMPLATE",
]
