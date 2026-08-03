"""LLM relationship recommendations constrained to catalog-approved field IDs."""

from __future__ import annotations

import json
from dataclasses import asdict

from pydantic import Field

from data_agent.runtime.dependencies import ModelClient
from data_agent.tools.schemas import CatalogSnapshot, NonBlankText

from .candidates import RelationshipCandidate, prefilter_candidates
from .models import RelationshipModel
from .profiler import PairProfile


class LlmRelationshipRecommendation(RelationshipModel):
    from_relation_id: NonBlankText
    from_column_id: NonBlankText
    to_relation_id: NonBlankText
    to_column_id: NonBlankText
    cardinality_hint: str = "unknown"
    confidence: float = Field(ge=0, le=1)
    explanation: NonBlankText


class RelationshipRecommender:
    """Use an ID allowlist, bounded batches, one repair, and deterministic merge."""

    prompt_version = "relationship-v2"

    profiler_version = "profile-v1"

    def __init__(self, *, batch_size: int = 100, profiler_version: str | None = None) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._batch_size = batch_size
        self._profiler_version = profiler_version or self.profiler_version
        self._cache: dict[tuple[str, str, str, str, str], tuple[LlmRelationshipRecommendation, ...]] = {}

    async def recommend(
        self,
        *,
        catalog: CatalogSnapshot,
        model_client: ModelClient,
        pair_profiles: dict[tuple[str, str], PairProfile] | None = None,
    ) -> tuple[LlmRelationshipRecommendation, ...]:
        candidates = prefilter_candidates(catalog, pair_profiles=pair_profiles)
        if not candidates:
            return ()
        cache_key = (
            catalog.schema_fingerprint,
            str(getattr(model_client, "model_id", "unknown")),
            str(getattr(model_client, "version", "unknown")),
            self.prompt_version,
            self._profiler_version,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        allowed = {
            (item.from_relation_id, item.from_column_id, item.to_relation_id, item.to_column_id)
            for item in candidates
        }
        recommended: list[LlmRelationshipRecommendation] = []
        for offset in range(0, len(candidates), self._batch_size):
            batch = candidates[offset : offset + self._batch_size]
            recommended.extend(
                await self._recommend_batch(
                    batch=batch,
                    allowed=allowed,
                    model_client=model_client,
                )
            )
        # Conflicting duplicates do not become additional edges. The highest
        # confidence survives; the lexical tie-breaker is stable across runs.
        merged: dict[tuple[str, str, str, str], LlmRelationshipRecommendation] = {}
        for item in recommended:
            key = (
                item.from_relation_id,
                item.from_column_id,
                item.to_relation_id,
                item.to_column_id,
            )
            previous = merged.get(key)
            if previous is None or (item.confidence, item.explanation) > (
                previous.confidence,
                previous.explanation,
            ):
                merged[key] = item
        result = tuple(merged[key] for key in sorted(merged))
        self._cache[cache_key] = result
        return result

    async def _recommend_batch(
        self,
        *,
        batch: tuple[RelationshipCandidate, ...],
        allowed: set[tuple[str, str, str, str]],
        model_client: ModelClient,
    ) -> tuple[LlmRelationshipRecommendation, ...]:
        prompt = self._prompt(batch)
        raw = await model_client.complete(
            prompt,
            system="You produce strict safe JSON.",
            max_output_tokens=2048,
        )
        parsed = self._parse(raw, allowed)
        if parsed is not None:
            return parsed
        repaired = await model_client.complete(
            json.dumps(
                {
                    "task": "repair_relationship_recommendations",
                    "instructions": "Return only valid JSON matching the supplied schema and candidate ID allowlist. Do not return SQL, expressions, values, or prose.",
                    "previous_response": raw[:4000],
                    "candidates": [asdict(item) for item in batch],
                    "schema": {"recommendations": [LlmRelationshipRecommendation.model_json_schema()]},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            system="You produce strict safe JSON.",
            max_output_tokens=2048,
        )
        return self._parse(repaired, allowed) or ()

    @staticmethod
    def _prompt(batch: tuple[RelationshipCandidate, ...]) -> str:
        return json.dumps(
            {
                "task": "recommend_relationships",
                "instructions": "Return JSON object with recommendations only. Select only candidate IDs. Do not return SQL, expressions, raw values, or identifiers outside candidates.",
                "candidates": [asdict(item) for item in batch],
                "schema": {"recommendations": [LlmRelationshipRecommendation.model_json_schema()]},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _parse(
        raw: str,
        allowed: set[tuple[str, str, str, str]],
    ) -> tuple[LlmRelationshipRecommendation, ...] | None:
        try:
            document = json.loads(raw)
            items = document.get("recommendations", [])
            if not isinstance(items, list):
                return None
            recommendations = tuple(
                LlmRelationshipRecommendation.model_validate(item)
                for item in items
            )
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return tuple(
            item
            for item in recommendations
            if (
                item.from_relation_id,
                item.from_column_id,
                item.to_relation_id,
                item.to_column_id,
            )
            in allowed
        )


__all__ = ["LlmRelationshipRecommendation", "RelationshipRecommender"]
