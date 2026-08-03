"""Bounded, data-free candidate prefiltering for relationship discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from data_agent.tools.schemas import CatalogSnapshot

from .profiler import PairProfile


@dataclass(frozen=True, slots=True)
class RelationshipCandidate:
    from_relation_id: str
    from_column_id: str
    to_relation_id: str
    to_column_id: str
    reason_code: str


def prefilter_candidates(
    catalog: CatalogSnapshot,
    *,
    per_relation_pair: int = 5,
    pair_profiles: Mapping[tuple[str, str], PairProfile] | None = None,
) -> tuple[RelationshipCandidate, ...]:
    """Prefer FKs, keys, and bounded value-free profile evidence.

    ``pair_profiles`` contains only summary statistics.  It permits different
    column names with compatible semantics/value domains to reach the LLM
    allowlist, while a demonstrated low overlap is excluded before prompting.
    """

    candidates: list[RelationshipCandidate] = []
    for relation in catalog.relations:
        for foreign_key in relation.foreign_keys:
            candidates.extend(
                RelationshipCandidate(relation.relation_id, left, foreign_key.to_relation_id, right, "DECLARED_FOREIGN_KEY")
                for left, right in zip(foreign_key.from_column_ids, foreign_key.to_column_ids, strict=True)
            )
    for index, left in enumerate(catalog.relations):
        for right in catalog.relations[index + 1:]:
            pair: list[RelationshipCandidate] = []
            left_key_ids = {
                column_id
                for key in left.keys
                for column_id in key.column_ids
            }
            right_key_ids = {
                column_id
                for key in right.keys
                for column_id in key.column_ids
            }
            for left_column in left.columns:
                for right_column in right.columns:
                    if _family(left_column.data_type) != _family(right_column.data_type):
                        continue
                    profile = _pair_profile(pair_profiles, left_column.column_id, right_column.column_id)
                    if profile is not None and profile.match_rate is not None and profile.match_rate < 0.2:
                        continue
                    if (
                        left_column.column_id in left_key_ids
                        or right_column.column_id in right_key_ids
                    ):
                        pair.append(
                            RelationshipCandidate(
                                left.relation_id,
                                left_column.column_id,
                                right.relation_id,
                                right_column.column_id,
                                "KEY_TYPE_COMPATIBLE",
                            )
                        )
                    if _name(left_column.name) == _name(right_column.name):
                        pair.append(RelationshipCandidate(left.relation_id, left_column.column_id, right.relation_id, right_column.column_id, "NORMALIZED_NAME_MATCH"))
                    elif (
                        profile is not None
                        and profile.type_compatible
                        and profile.match_rate is not None
                        and profile.match_rate >= 0.8
                        and profile.normalized_name_similarity > 0
                    ):
                        pair.append(RelationshipCandidate(left.relation_id, left_column.column_id, right.relation_id, right_column.column_id, "PROFILE_SEMANTIC_VALUE_MATCH"))
            seen: set[tuple[str, str, str, str]] = set()
            for candidate in pair:
                key = (
                    candidate.from_relation_id,
                    candidate.from_column_id,
                    candidate.to_relation_id,
                    candidate.to_column_id,
                )
                if key not in seen:
                    seen.add(key)
                    candidates.append(candidate)
                if len(seen) >= per_relation_pair:
                    break
    seen: set[tuple[str, str, str, str]] = set()
    result: list[RelationshipCandidate] = []
    for candidate in candidates:
        key = (
            candidate.from_relation_id,
            candidate.from_column_id,
            candidate.to_relation_id,
            candidate.to_column_id,
        )
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return tuple(result)


def _pair_profile(
    profiles: Mapping[tuple[str, str], PairProfile] | None,
    left_column_id: str,
    right_column_id: str,
) -> PairProfile | None:
    if profiles is None:
        return None
    return profiles.get((left_column_id, right_column_id)) or profiles.get(
        (right_column_id, left_column_id)
    )


def _name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold()).removesuffix("id")


def _family(value: str) -> str:
    value = value.casefold()
    if any(item in value for item in ("int", "decimal", "numeric", "real", "double", "float")):
        return "number"
    if any(item in value for item in ("date", "time")):
        return "temporal"
    return "text"


__all__ = ["RelationshipCandidate", "prefilter_candidates"]
