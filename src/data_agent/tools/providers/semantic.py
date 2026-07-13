"""Deterministic semantic search over the governed DomainPack snapshot."""

from __future__ import annotations

import re

from data_agent.runtime.packs import DomainPack

from ..models import ProviderContext, RetryPolicy, ToolSpec
from .contracts import (
    SemanticKind,
    SemanticMatch,
    SemanticSearchInput,
    SemanticSearchOutput,
)


SEMANTIC_SEARCH_SPEC = ToolSpec(
    name="semantic.search",
    version="1.0.0",
    description="Search governed canonical entities, fields, metrics, relationships, and policies.",
    input_schema=SemanticSearchInput,
    output_schema=SemanticSearchOutput,
    risk_level="low",
    side_effects="none",
    required_capabilities=("semantic.search",),
    idempotency="safe",
    timeout_seconds=2,
    retry_policy=RetryPolicy(max_attempts=1),
    eval_tags=("semantic", "offline"),
)


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        item
        for item in re.split(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", value.casefold())
        if item
    )


def _score(query: str, text: str) -> float:
    normalized_query = query.casefold().strip()
    normalized_text = text.casefold()
    if normalized_query == normalized_text:
        return 1.0
    if normalized_query in normalized_text:
        return 0.9
    query_tokens = _tokens(normalized_query)
    text_tokens = _tokens(normalized_text)
    if not query_tokens:
        return 0.0
    overlap = len(query_tokens.intersection(text_tokens))
    return min(0.8, overlap / len(query_tokens)) if overlap else 0.0


class SemanticSearchProvider:
    spec = SEMANTIC_SEARCH_SPEC

    def __init__(self, domain_pack: DomainPack) -> None:
        self._domain = domain_pack

    async def invoke(
        self,
        payload: SemanticSearchInput,
        context: ProviderContext,
    ) -> SemanticSearchOutput:
        candidates: list[tuple[str, SemanticKind, str, str]] = []
        for entity_ref, entity in self._domain.spec.entities.items():
            candidates.append(
                (
                    entity_ref,
                    SemanticKind.ENTITY,
                    entity_ref.rsplit(".", 1)[-1],
                    entity.description or "",
                )
            )
            for field_name, field in entity.fields.items():
                candidates.append(
                    (
                        f"{entity_ref}.{field_name}",
                        SemanticKind.FIELD,
                        field_name,
                        field.description or "",
                    )
                )
        for metric_ref, metric in self._domain.spec.metrics.items():
            candidates.append(
                (
                    metric_ref,
                    SemanticKind.METRIC,
                    metric_ref.rsplit(".", 1)[-1],
                    metric.description or "",
                )
            )
        for relationship in self._domain.spec.relationships:
            candidates.append(
                (
                    relationship.name,
                    SemanticKind.RELATIONSHIP,
                    relationship.name.rsplit(".", 1)[-1],
                    f"{relationship.from_entity} to {relationship.to_entity}",
                )
            )
        for policy in self._domain.spec.policies:
            candidates.append(
                (
                    policy.name,
                    SemanticKind.POLICY,
                    policy.name.rsplit(".", 1)[-1],
                    policy.description,
                )
            )
        for vocabulary in self._domain.spec.vocabulary:
            for ref in vocabulary.refs:
                kind = self._kind_for_ref(ref)
                candidates.append((ref, kind, vocabulary.term, vocabulary.term))

        allowed_kinds = set(payload.kinds)
        best: dict[str, SemanticMatch] = {}
        for ref, kind, label, description in candidates:
            if allowed_kinds and kind not in allowed_kinds:
                continue
            score = max(
                _score(payload.query, ref),
                _score(payload.query, label),
                _score(payload.query, description),
            )
            if score <= 0:
                continue
            candidate = SemanticMatch(
                ref=ref,
                kind=kind,
                label=label,
                description=description,
                score=score,
            )
            current = best.get(ref)
            if current is None or (candidate.score, candidate.label) > (
                current.score,
                current.label,
            ):
                best[ref] = candidate
        ordered = sorted(
            best.values(),
            key=lambda item: (-item.score, item.ref, item.label),
        )
        return SemanticSearchOutput(matches=tuple(ordered[: payload.limit]))

    def _kind_for_ref(self, ref: str) -> SemanticKind:
        if ref in self._domain.spec.entities:
            return SemanticKind.ENTITY
        if ref in self._domain.spec.metrics:
            return SemanticKind.METRIC
        if ref in {item.name for item in self._domain.spec.relationships}:
            return SemanticKind.RELATIONSHIP
        if ref in {item.name for item in self._domain.spec.policies}:
            return SemanticKind.POLICY
        return SemanticKind.FIELD
